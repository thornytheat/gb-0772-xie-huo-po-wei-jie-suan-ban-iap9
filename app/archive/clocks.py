"""候选校时事件的登记、单调生效与重算应收。

校时记录可能晚到、可能被重复重跑。结论只承认更新的校时版本：

* 更新的 seq/时刻到达 -> 重算并替换当前候选结论；
* 同号重跑 -> 幂等复用，不新增结论；
* 更旧的校时在新校时生效后重跑 -> ``STALE``，当前结论（含已确认的重算应收）不变。

状态以 JSON 存在 SQLite 的独立表中，不进入清算留档摘要，也不会把明文车牌
带进归档或理赔导出。
"""

from contextlib import closing, contextmanager
import json
import sqlite3
from datetime import UTC, datetime

from app.archive.canonical import serialize
from app.curb.timeaxis import minute

APPLIED = 'APPLIED'
STALE = 'STALE'
DUPLICATE = 'DUPLICATE'
CONFIRMED = 'CONFIRMED'

NUMERIC_KEYS = ('seq', 'sequence', 'version', 'revision', 'clock_seq', 'order_no', 'n')
TIME_KEYS = ('issued_at', 'effective_at', 'recorded_at', 'received_at', 'at', 'timestamp', 'time')
ID_KEYS = ('clock_id', 'calibration_id', 'revision_id', 'id')
TARGET_KEYS = ('stay', 'stay_id', 'order_id', 'target', 'target_id', 'event', 'event_id', 'record', 'record_id')
SHIFT_KEYS = ('shift_minutes', 'arrival_shift_minutes', 'minutes', 'offset_minutes', 'offset', 'delta', 'drift')


def order_of(event):
    """返回可比较的校时版本序号；优先显式整数版本，其次签发时刻。"""
    if not isinstance(event, dict):
        return None
    for key in NUMERIC_KEYS:
        if event.get(key) is not None:
            try:
                return (0, int(event[key]), '')
            except (TypeError, ValueError):
                continue
    for key in TIME_KEYS:
        if event.get(key):
            try:
                when = minute(str(event[key]))
                return (1, int(when.timestamp()), when.isoformat())
            except (ValueError, TypeError):
                continue
    return None


def event_identity(event):
    for key in ID_KEYS:
        if event.get(key):
            return str(event[key])
    return None


def extract_corrections(body):
    """从多种合理请求形态里取出校时修正列表。"""
    if not isinstance(body, dict):
        return [], {}
    raw = None
    for key in ('corrections', 'calibrations', 'clocks', 'shifts', 'items', 'candidates'):
        value = (body or {}).get(key)
        if isinstance(value, (list, dict)):
            raw = value
            break
    corrections = []
    if isinstance(raw, dict):
        for target, spec in raw.items():
            item = {'stay': target}
            if isinstance(spec, dict):
                item.update(spec)
            else:
                item['shift_minutes'] = spec
            corrections.append(item)
    elif raw:
        corrections = [dict(x) for x in raw if isinstance(x, dict)]
    target = next((body[k] for k in TARGET_KEYS if body.get(k) is not None), None)
    shift = next((body[k] for k in SHIFT_KEYS if body.get(k) is not None), None)
    if target is not None:
        single = {'stay': target}
        if shift is not None:
            single['shift_minutes'] = shift
        for key in ('start', 'arrival', 'at', 'to', 'corrected_at', 'corrected_start'):
            if body.get(key):
                single['start'] = body[key]
                break
        corrections.append(single)
    meta = {k: body[k] for k in ('seq', 'sequence', 'version', 'revision') if k in body}
    for key in TIME_KEYS + ID_KEYS:
        if key in body:
            meta[key] = body[key]
    return corrections, meta


def receive(state, event, recompute):
    """纯归约：把一条校时事件应用到当前状态。

    ``recompute(corrections) -> {'review':..., 'recalculation':...}`` 由持久层
    提供，只有在确实是更新校时时才会调用。
    """
    state = state or {'seq_order': None, 'event_id': None, 'current': None,
                      'confirmed_order': None, 'conclusion': None, 'log': []}
    oid = event_identity(event)
    order = order_of(event)
    if oid and any(row.get('id') == oid for row in state.get('log', [])):
        row = next(row for row in state['log'] if row.get('id') == oid)
        return state, {'status': DUPLICATE, 'changed': False, 'stale': False, 'reused': True,
                       'reason': 'CLOCK_DUPLICATE', 'event': row}
    current_order = state.get('seq_order')
    if order is not None and current_order is not None and order <= tuple(current_order):
        outcome = {'status': STALE, 'changed': False, 'stale': True, 'reused': False,
                   'reason': 'CLOCK_OLD_IGNORED', 'current_seq': state.get('seq'),
                   'confirmed': state.get('confirmed_order') is not None}
        state.setdefault('log', []).append({'id': oid, 'order': list(order), 'status': STALE})
        return state, outcome

    corrections = event.get('corrections')
    if corrections is None:
        corrections, _ = extract_corrections(event)
    computed = recompute(corrections)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    brief = {'id': oid, 'order': list(order) if order else None, 'received_utc': stamp,
             'corrections': corrections, 'status': APPLIED}
    state['current'] = dict(brief, review=computed['review'], recalculation=computed.get('recalculation'))
    state['seq_order'] = list(order) if order else None
    state['seq'] = order[1] if order else state.get('seq', 0) + 1
    state['event_id'] = oid
    # 新校时成为当前候选；若此前已确认旧结论，需要重新确认才改写正式应收。
    state['log'] = state.get('log', []) + [brief]
    outcome = {'status': APPLIED, 'changed': True, 'stale': False, 'reused': False,
               'reason': 'CLOCK_APPLIED', 'seq': state['seq'], 'applied_utc': stamp}
    return state, outcome


def confirm(state, body, recompute):
    """确认某版校时；旧版确认不能覆盖已生效的新版结论。"""
    state = state or {'seq_order': None, 'current': None, 'conclusion': None, 'log': []}
    if not state.get('current'):
        return state, {'status': STALE, 'changed': False, 'stale': True,
                       'reason': 'CLOCK_NONE'}
    target_order = order_of(body or {})
    current_order = tuple(state['seq_order']) if state.get('seq_order') else None
    confirmed_order = tuple(state['confirmed_order']) if state.get('confirmed_order') else None
    if target_order is not None:
        if current_order is not None and target_order < current_order:
            return state, {'status': STALE, 'changed': False, 'stale': True,
                           'reason': 'CLOCK_OLD_IGNORED', 'seq': state.get('seq')}
        if confirmed_order is not None and target_order <= confirmed_order and not body.get('force'):
            return state, {'status': DUPLICATE, 'changed': False, 'stale': False, 'reused': True,
                           'reason': 'CLOCK_ALREADY_CONFIRMED'}
    recalculation = state['current'].get('recalculation')
    if recalculation is None:
        corrections = state['current'].get('corrections') or []
        recalculation = recompute(corrections).get('recalculation')
        state['current']['recalculation'] = recalculation
    state['conclusion'] = recalculation
    state['confirmed_order'] = list(current_order) if current_order else None
    state['confirmed_seq'] = state.get('seq')
    return state, {'status': CONFIRMED, 'changed': True, 'stale': False,
                   'reason': 'CLOCK_CONFIRMED', 'seq': state.get('seq'),
                   'recalculation': recalculation}


class ClockStore:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS clock_records(
                scope TEXT PRIMARY KEY, content TEXT NOT NULL, updated_utc TEXT NOT NULL)''')

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            yield db

    def load(self, scope):
        with self.connect() as db:
            row = db.execute('SELECT content FROM clock_records WHERE scope=?', (scope,)).fetchone()
        return None if row is None else json.loads(row[0])

    def save(self, scope, state):
        payload = serialize(state)
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute('INSERT INTO clock_records(scope,content,updated_utc) VALUES(?,?,?) '
                       'ON CONFLICT(scope) DO UPDATE SET content=excluded.content, updated_utc=excluded.updated_utc',
                       (scope, payload, stamp))
        return state


def public_review(result):
    """去掉含 datetime/元组键的原始场景，保留可直接 JSON 化的结论。"""
    return {key: value for key, value in result.items() if key not in ('baseline', 'candidate')}


def recalculation(result, archived_settlement=None):
    """把重算应收与留档实收按稳定 stay 编号勾连。"""
    paid_by_stay = {}
    if archived_settlement:
        paid_by_stay = {line['stay']: line for line in archived_settlement.get('lines', [])}
    lines = []
    for verdict in result['lines']:
        original = paid_by_stay.get(verdict['stay'], {})
        paid = original.get('paid_cents', 0) or 0
        due = verdict['due_after_cents']
        lines.append({
            'stay': verdict['stay'], 'berth': verdict['berth'], 'vehicle': verdict['vehicle'],
            'arrival': verdict['arrival_after'],
            'due_cents': due,
            'due_before_cents': verdict['due_before_cents'],
            'original_due_cents': verdict['due_before_cents'],
            'recalculated_due_cents': due,
            'paid_cents': paid,
            'difference_cents': paid - due if due is not None else None,
            'settleable': verdict['settleable'], 'hold': verdict['hold'],
            'status': verdict['status'], 'reasons': verdict['reasons'],
            'receipts': original.get('receipts', []),
            'payment_code': original.get('code'),
        })
    money_due = sum(line['due_cents'] or 0 for line in lines)
    money_paid = sum(line['paid_cents'] for line in lines)
    return {
        'lines': lines,
        'money': {'due_cents': money_due, 'paid_cents': money_paid,
                  'difference_cents': money_paid - money_due},
        'due_cents': money_due,
        'settle_count': result['settle_count'], 'hold_count': result['hold_count'],
    }


def _coerce_stops(stops):
    rows = []
    for s in stops:
        row = dict(s)
        for key in ('start', 'end'):
            if isinstance(row.get(key), str):
                row[key] = minute(row[key])
        rows.append(row)
    return rows


def _coerce_histories(histories):
    if not isinstance(histories, dict):
        return histories
    ready = {}
    for reservation, rows in histories.items():
        group = []
        for row in rows:
            item = dict(row)
            for key in ('start', 'end', 'effective'):
                if isinstance(item.get(key), str):
                    item[key] = minute(item[key])
            group.append(item)
        ready[reservation] = group
    return ready


def _histories_from_evidence(record):
    histories = {}
    for stop in record['evidence'].get('identity', {}).get('stays', []):
        for row in stop.get('candidates', []):
            item = dict(row)
            for key in ('start', 'end', 'effective'):
                if isinstance(item.get(key), str):
                    item[key] = minute(item[key])
            histories.setdefault(item['reservation'], []).append(item)
    return histories


def recompute_for_docket(docket, key, corrections):
    """优先用原始单据完整重算；缺失时退回留档证据里的候选预约。"""
    from app.clearing import Clearing
    from app.source.packet import bundle

    record = docket.require(key)
    files = docket.load_inputs(key)
    if files is None and record['manifest'].get('source') in ('dirty', 'clean'):
        files = bundle(record['manifest']['source'])
    if files is not None:
        clearing = Clearing(files, docket.secret)
        result = clearing.clock_review(corrections)
        settlement = record['evidence'].get('settlement')
    else:
        from app.curb.calibrate import assess
        stops = _coerce_stops(record['evidence']['occupancy']['stays'])
        histories = _histories_from_evidence(record)
        result = assess(stops, histories=histories, corrections=corrections)
        settlement = record['evidence'].get('settlement')
    return {'review': public_review(result), 'recalculation': recalculation(result, settlement)}


def stateless_review(body, secret):
    """不落库的双时刻复核，支持 sample/bundle/原始 stops 等请求形态。"""
    from app.curb.calibrate import assess
    from app.source.packet import bundle

    corrections, _ = extract_corrections(body or {})
    move = (body or {}).get('move', 'both')
    if (body or {}).get('arrival_only') is True:
        move = 'start'

    if body.get('sample') or body.get('bundle'):
        from app.clearing import Clearing
        files = bundle(body['sample']) if body.get('sample') else body['bundle']
        clearing = Clearing(files, secret)
        result = clearing.clock_review(corrections, move=move)
        archived = None
    else:
        raw = body.get('stops') or body.get('before') or body.get('current')
        after_raw = body.get('after') or body.get('candidate') or body.get('candidate_stops')
        if not raw:
            from app.problems import DocketError
            raise DocketError('REVIEW_INPUT_BAD', '需提供 sample、bundle 或 stops 与校时修正')
        stops = _coerce_stops(raw)
        after = _coerce_stops(after_raw) if after_raw else None
        histories = body.get('histories')
        if histories is None and isinstance(body.get('bookings'), dict):
            histories = body['bookings']
        if isinstance(body.get('bookings'), list) and histories is None:
            histories = {}
            for row in body['bookings']:
                histories.setdefault(row.get('reservation'), []).append(row)
        histories = _coerce_histories(histories)
        result = assess(stops, after=after, histories=histories,
                        corrections=None if after is not None else corrections, move=move)
        archived = body.get('settlement')
    payload = public_review(result)
    if archived:
        payload['recalculation'] = recalculation(result, archived)
    return payload
