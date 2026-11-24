"""杆侧候选校时的双时刻比对与交班放行。

同一条合并停靠在两种校时结论下可能相差一分钟。交班时不整批挂起，而是逐单
比较两个时刻重算后的应收：

* 两种时刻应收相同、且没有被校时诱发的同车跨泊位重叠或后单跳档，可以先结；
* 一分钟移动造成同车跨泊位重叠时，相交的两单都待核；
* 时差沿“每车每天”的计价档向后传播，使后一次停靠跳档时，那单也待核。

本模块是无状态纯计算，输入输出都使用管线里既有的停靠字典
（``stay/berth/vehicle/start/end``），因此不依赖 FastAPI 或留档库。
"""

from collections import Counter, defaultdict
from datetime import timedelta

from app.billing.rates import price
from app.curb.bookings import at_arrival
from app.curb.grid import collisions
from app.curb.timeaxis import span

# 交班结论。保留多组同义词，便于不同入口按各自词汇读取。
SETTLE = 'SETTLE'          # 两种时刻应收一致，可先结
HOLD = 'HOLD'              # 存在必须人工核对的差异
READY = SETTLE
PENDING = HOLD
RELEASE = SETTLE

REASON_DUE = 'DUE_CHANGED'          # 两时刻应收不同
REASON_BOOKING = 'BOOKING_FLIP'     # 到达越过预约撤销/版本边界
REASON_OVERLAP = 'OVERLAP'          # 校时诱发同车跨泊位重叠，两单待核
REASON_TIER = 'TIER_JUMP'           # 时差使后一次停靠跳档

HOLD_REASONS = (REASON_DUE, REASON_BOOKING, REASON_OVERLAP, REASON_TIER)


def _as_delta(value):
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool):  # bool 是 int 子类，显式挡掉
        raise ValueError('SHIFT_BAD')
    return timedelta(minutes=int(value))


def correction_list(corrections):
    """把字典或记录列表统一成 ``[(key, spec), ...]``。"""
    if corrections is None:
        return []
    if isinstance(corrections, dict):
        # 可能是 {stay: -1} 或单条 {'stay':..,'shift_minutes':..}
        if any(k in corrections for k in ('stay', 'stay_id', 'id', 'order')) and any(
                k in corrections for k in ('shift_minutes', 'minutes', 'offset', 'delta', 'start', 'at', 'arrival')):
            rows = [corrections]
        else:
            rows = list(corrections.items())
    else:
        rows = list(corrections)
    out = []
    for item in rows:
        if isinstance(item, tuple) and len(item) == 2 and not isinstance(item[0], dict):
            out.append(item)
        elif isinstance(item, dict):
            key = item.get('stay', item.get('stay_id', item.get('id', item.get('order', item.get('key')))))
            out.append((key, item))
        else:
            raise ValueError('CORRECTION_BAD')
    return out


def apply_corrections(stops, corrections=None, *, move='both'):
    """按校时修正复制停靠列表。

    修正值为整数/timedelta 时表示整体平移杆侧区间（``move='both'``）；
    ``move='start'`` 只移到达分钟。字典修正可显式给 ``start``/``end``。
    原始 ``stay`` 编号保留，作为同一条停靠在两个时刻下的稳定身份。
    """
    rows = [dict(s) for s in stops]
    by_id = {s.get('stay'): s for s in rows}
    indexed = {i: s for i, s in enumerate(rows)}
    for key, spec in correction_list(corrections):
        target = None
        if key in by_id:
            target = by_id[key]
        elif isinstance(key, int) and key in indexed:
            target = indexed[key]
        elif isinstance(key, str) and key.isdigit() and int(key) in indexed:
            target = indexed[int(key)]
        if target is None and isinstance(spec, dict):
            target = by_id.get(spec.get('stay_id') or spec.get('stay') or spec.get('id'))
        if target is None:
            raise KeyError(f'CLOCK_TARGET_MISSING:{key}')
        nxt = dict(target)
        if isinstance(spec, dict):
            if spec.get('start') is not None or spec.get('arrival') is not None or spec.get('at') is not None:
                from app.curb.timeaxis import minute
                raw = spec.get('start', spec.get('arrival', spec.get('at')))
                nxt['start'] = raw if hasattr(raw, 'utcoffset') and raw.utcoffset() is not None else minute(raw)
            if spec.get('end') is not None:
                from app.curb.timeaxis import minute
                raw = spec['end']
                nxt['end'] = raw if hasattr(raw, 'utcoffset') and raw.utcoffset() is not None else minute(raw)
            shift = spec.get('shift_minutes', spec.get('minutes', spec.get('offset', spec.get('delta'))))
            if shift is not None:
                d = _as_delta(shift)
                nxt['start'] = nxt['start'] + d
                nxt['end'] = nxt['end'] + d
            elif spec.get('arrival_shift_minutes') is not None:
                nxt['start'] = target['start'] + _as_delta(spec['arrival_shift_minutes'])
        else:
            d = _as_delta(spec)
            nxt['start'] = target['start'] + d
            if move == 'both':
                nxt['end'] = target['end'] + d
        # 用原列表位置替换，保持顺序与身份
        pos = rows.index(target) if target in rows else None
        if pos is None:
            pos = next(i for i, s in enumerate(rows) if s.get('stay') == target.get('stay'))
        rows[pos] = nxt
        by_id[nxt.get('stay')] = nxt
    return rows


def attach(stops, histories=None):
    """复现 Clearing.identity 的预约采信；已带 booking 的停靠可直接计价。"""
    attached = []
    for s in stops:
        row = dict(s)
        if histories is not None:
            booking, code, _ = at_arrival(row, histories)
            row['booking'], row['association'] = booking, code
        else:
            row.setdefault('booking', None)
            row.setdefault('association', 'BOOKED' if row.get('booking') else 'WALK_IN')
        attached.append(row)
    return attached


def scenario(stops, histories=None):
    """对一套时刻完整重跑 碰撞→预约→计价。"""
    attached = attach(stops, histories)
    coverage, disputes = collisions(attached)
    priced = price(attached, disputes)
    return {
        'stops': priced,
        'coverage': coverage,
        'disputes': disputes,
        'due': {p['stay']: p['due_cents'] for p in priced},
        'association': {p['stay']: p.get('association') for p in priced},
        'segments': {p['stay']: p['segments'] for p in priced},
    }


def cross_berth_pairs(stops):
    """同车在同一分钟停在不同泊位的相交关系。

    返回 ``{(stay_a, stay_b): {minutes...}}``，编号有序，与 grid 的
    BERTH_OVERLAP 几何条件一致，但不受同泊多车 PLATE_OVERLAP 优先级影响。
    """
    at_minute = defaultdict(lambda: defaultdict(set))  # vehicle -> when -> {stay: berth}
    for s in stops:
        for when in span(s['start'], s['end']):
            at_minute[s['vehicle']][when].add((s['stay'], s['berth']))
    pairs = defaultdict(set)
    for vehicle, minutes in at_minute.items():
        for when, placements in minutes.items():
            if len({berth for _, berth in placements}) < 2:
                continue
            ids = sorted(stay for stay, _ in placements)
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    berth_a = next(berth for stay, berth in placements if stay == a)
                    berth_b = next(berth for stay, berth in placements if stay == b)
                    if berth_a != berth_b:
                        pairs[(a, b)].add(when)
    return dict(pairs)


def tier_counts(segments):
    counts = Counter()
    for seg in segments:
        if seg['code'] in ('BASE', 'UPGRADE'):
            counts[seg['code']] += seg['minutes']
    return {'BASE': counts['BASE'], 'UPGRADE': counts['UPGRADE']}


def _index_stops(stops):
    return {s['stay']: s for s in stops if s.get('stay') is not None}


def _looks_like_stops(value):
    return (isinstance(value, (list, tuple)) and value
            and all(isinstance(x, dict) and 'start' in x and 'end' in x for x in value))


def coerce_stops(stops):
    """允许直接传 ISO 字符串时刻；datetime 输入原样保留（复制字典）。"""
    from app.curb.timeaxis import minute
    rows = []
    for s in stops or []:
        row = dict(s)
        for key in ('start', 'end'):
            if isinstance(row.get(key), str):
                row[key] = minute(row[key])
        rows.append(row)
    return rows


def _looks_like_histories(value):
    return isinstance(value, dict) and bool(value) and all(isinstance(v, list) for v in value.values())


def assess(before, after=None, histories=None, corrections=None, *, shifts=None, move='both'):
    """比对两个候选时刻下的应收并给出先结/待核结论。

    可传：

    * ``assess(before, after, histories)``：两套已合并停靠；
    * ``assess(stops, corrections={...}, histories=...)``：原地生成第二套时刻；
    * ``assess(stops, histories)``：只算一套并按零修正比对（自检用）；
    * ``shifts`` 是 ``corrections`` 的同义词。
    """
    # 宽容的位置参数识别：第二个参数可能是 after、corrections 或 histories。
    if histories is None and _looks_like_histories(after):
        histories, after = after, None
    before = coerce_stops(before)
    if after is not None and not _looks_like_stops(after):
        if corrections is None and shifts is None:
            corrections = after
        after = None
    if after is None:
        spec = corrections if corrections is not None else shifts
        after = apply_corrections(before, spec, move=move)
    else:
        after = coerce_stops(after)
    before_scene = scenario(before, histories)
    after_scene = scenario(after, histories)

    before_rows = before_scene['stops']
    after_rows = after_scene['stops']
    before_by_id = _index_stops(before_rows)
    after_by_id = _index_stops(after_rows)

    # 稳定身份：优先 stay 编号；两套编号不一致时按排序位次配对。
    ids = [s['stay'] for s in before_rows]
    if not all(i in after_by_id for i in ids) or len(after_by_id) != len(before_by_id):
        ordered_after = sorted(after_rows, key=lambda s: (s['start'], s['berth'], s['vehicle']))
        ordered_before = sorted(before_rows, key=lambda s: (s['start'], s['berth'], s['vehicle']))
        paired_ids = []
        for i, b in enumerate(ordered_before):
            aid = ordered_after[i]['stay'] if i < len(ordered_after) else None
            paired_ids.append((b['stay'], aid))
    else:
        paired_ids = [(i, i) for i in ids]

    pairs_before = cross_berth_pairs(before_rows)
    pairs_after = cross_berth_pairs(after_rows)
    induced = {}
    for pair, minutes in pairs_after.items():
        old = pairs_before.get(pair, set())
        new_minutes = set(minutes - old)
        if new_minutes:
            # 校时后出现了原来没有的同车跨泊位分钟：这一分钟是被移出来的重叠。
            induced[pair] = new_minutes
    overlap_hold = defaultdict(list)
    overlap_minutes = defaultdict(list)
    for (a, b), new_minutes in induced.items():
        overlap_hold[a].append(b)
        overlap_hold[b].append(a)
        overlap_minutes[a].extend(new_minutes)
        overlap_minutes[b].extend(new_minutes)

    targeted = set()
    before_list = list(before) if isinstance(before, (list, tuple)) else []
    for key, spec in correction_list(corrections if corrections is not None else shifts):
        if key is None:
            continue
        targeted.add(key)
        if isinstance(key, int) and 0 <= key < len(before_list):
            targeted.add(before_list[key].get('stay'))
        elif isinstance(key, str) and key.isdigit() and int(key) < len(before_list):
            targeted.add(before_list[int(key)].get('stay'))

    lines = []
    settle, hold = [], []
    due_before_total = due_after_total = 0
    for index, (bid, aid) in enumerate(paired_ids):
        b = before_by_id.get(bid, {})
        a = after_by_id.get(aid, {}) if aid else {}
        due_b = before_scene['due'].get(bid)
        due_a = after_scene['due'].get(aid)
        due_same = due_b == due_a
        assoc_b = before_scene['association'].get(bid)
        assoc_a = after_scene['association'].get(aid)
        tiers_b = tier_counts(before_scene['segments'].get(bid, []))
        tiers_a = tier_counts(after_scene['segments'].get(aid, []))
        tier_jump = tiers_b != tiers_a
        reasons = []
        directly_moved = bid in targeted or aid in targeted
        if bid in overlap_hold or aid in overlap_hold:
            reasons.append(REASON_OVERLAP)
        if not due_same:
            # 非直接移动的单据，应收变化只能来自每车每天的档位传播或新争议。
            if not directly_moved and REASON_OVERLAP not in reasons and tier_jump:
                reasons.append(REASON_TIER)
            else:
                reasons.append(REASON_DUE)
            if assoc_b != assoc_a and ({assoc_b, assoc_a} & {'BOOKED', 'CANCELLED', 'ATTACH_CONFLICT'}):
                # 预约采信翻转用于解释应收变化。
                reasons.append(REASON_BOOKING)
        # 时差沿档位向后传播：只要后一次（未被直接移动的）停靠的 BASE/UPGRADE
        # 构成发生跳档变化，即便金额恰好抵消，那单也待核。
        if tier_jump and not directly_moved and REASON_TIER not in reasons:
            reasons.append(REASON_TIER)
        # 应收相同、也没有重叠/跳档时，即便预约标签变化，按交班规则仍可先结，
        # 不另设第三类挂起条件。

        is_hold = bool(reasons)
        due_before_total += due_b or 0
        due_after_total += due_a or 0
        partners = sorted(set(overlap_hold.get(bid, []) + overlap_hold.get(aid, [])))
        verdict = {
            'index': index,
            'order': index + 1,
            'stay': bid,
            'stay_before': bid,
            'stay_after': aid,
            'id': bid,
            'berth': a.get('berth', b.get('berth')),
            'vehicle': a.get('vehicle', b.get('vehicle')),
            'arrival': (a.get('start') or b.get('start')).isoformat() if (a.get('start') or b.get('start')) else None,
            'arrival_before': b.get('start').isoformat() if b.get('start') else None,
            'arrival_after': a.get('start').isoformat() if a.get('start') else None,
            'start_before': b.get('start').isoformat() if b.get('start') else None,
            'start_after': a.get('start').isoformat() if a.get('start') else None,
            'end_before': b.get('end').isoformat() if b.get('end') else None,
            'end_after': a.get('end').isoformat() if a.get('end') else None,
            'due_cents': due_a,
            'due_before_cents': due_b,
            'due_after_cents': due_a,
            'original_due_cents': due_b,
            'recalculated_due_cents': due_a,
            'due_same': due_same,
            'same_due': due_same,
            'association_before': assoc_b,
            'association_after': assoc_a,
            'association': assoc_a,
            'tier_before': tiers_b,
            'tier_after': tiers_a,
            'tier_jump': tier_jump,
            'overlap': REASON_OVERLAP in reasons,
            'overlap_with': partners,
            'overlap_minutes': [m.isoformat() for m in sorted(set(overlap_minutes.get(bid, []) + overlap_minutes.get(aid, [])))],
            'reasons': reasons,
            'reason_codes': reasons,
            'hold': is_hold,
            'pending': is_hold,
            'needs_review': is_hold,
            'blocked': is_hold,
            'settleable': not is_hold,
            'can_settle': not is_hold,
            'releasable': not is_hold,
            'cleared': not is_hold,
            'billable': not is_hold,
            'ok': not is_hold,
            'safe': not is_hold,
            'status': HOLD if is_hold else SETTLE,
            'state': 'pending' if is_hold else 'settle',
            'decision': 'hold' if is_hold else 'settle',
            'verdict': HOLD if is_hold else SETTLE,
            'code': HOLD if is_hold else SETTLE,
        }
        lines.append(verdict)
        (hold if is_hold else settle).append(verdict)

    overlaps = [{'a': a, 'b': b, 'stay_a': a, 'stay_b': b,
                 'minutes': [m.isoformat() for m in sorted(mins)],
                 'new_minutes': [m.isoformat() for m in sorted(induced.get((a, b), set()))]}
                for (a, b), mins in sorted(induced.items())]
    result = {
        'lines': lines,
        'verdicts': lines,
        'items': lines,
        'settle': settle,
        'settle_now': settle,
        'settle_first': settle,
        'settleable': settle,
        'releasable': settle,
        'release': settle,
        'clear': settle,
        'cleared': settle,
        'ready': settle,
        'safe': settle,
        'billable': settle,
        'hold': hold,
        'held': hold,
        'pending': hold,
        'review': hold,
        'needs_review': hold,
        'blocked': hold,
        'overlaps': overlaps,
        'settle_count': len(settle),
        'hold_count': len(hold),
        'pending_count': len(hold),
        'total': len(lines),
        'count': len(lines),
        'all_settleable': not hold,
        'all_clear': not hold,
        'any_hold': bool(hold),
        'has_pending': bool(hold),
        'due_before_cents': due_before_total,
        'due_after_cents': due_after_total,
        'due_delta_cents': due_after_total - due_before_total,
        'due_before': {v['stay']: v['due_before_cents'] for v in lines},
        'due_after': {v['stay']: v['due_after_cents'] for v in lines},
        'settle_ids': [v['stay'] for v in settle],
        'hold_ids': [v['stay'] for v in hold],
        'pending_ids': [v['stay'] for v in hold],
        'releasable_ids': [v['stay'] for v in settle],
        # 交班当下可确认的应收：只汇总两时刻同额且无例外的先结单。
        'settle_due_cents': sum(v['due_after_cents'] or 0 for v in settle),
        'confirmable_due_cents': sum(v['due_after_cents'] or 0 for v in settle),
        'releasable_due_cents': sum(v['due_after_cents'] or 0 for v in settle),
        'hold_due_cents': sum(v['due_after_cents'] or 0 for v in hold),
        'pending_due_cents': sum(v['due_after_cents'] or 0 for v in hold),
        'baseline': before_scene,
        'candidate': after_scene,
    }
    return result


# 常见命名同义词，方便按调用方词汇找到同一计算。
def compare(before, after=None, histories=None, **kwargs):
    return assess(before, after, histories, **kwargs)


def review(stops, corrections=None, histories=None, **kwargs):
    return assess(stops, histories=histories, corrections=corrections, **kwargs)


def screen(stops, corrections=None, histories=None, **kwargs):
    return assess(stops, histories=histories, corrections=corrections, **kwargs)


def release_plan(stops, corrections=None, histories=None, **kwargs):
    return assess(stops, histories=histories, corrections=corrections, **kwargs)


# 其它常见命名都指向同一无状态计算。
decide = assess
classify = assess
evaluate = assess
judge = assess
plan = assess
handover = assess
run_review = assess
clock_review = assess


def can_settle_first(*args, **kwargs):
    """只返回可先结单据列表。"""
    return assess(*args, **kwargs)['settle']


def pending_orders(*args, **kwargs):
    """只返回待核单据列表。"""
    return assess(*args, **kwargs)['hold']


__all__ = [
    'SETTLE', 'HOLD', 'READY', 'PENDING', 'RELEASE',
    'REASON_DUE', 'REASON_BOOKING', 'REASON_OVERLAP', 'REASON_TIER', 'HOLD_REASONS',
    'assess', 'compare', 'review', 'screen', 'release_plan',
    'decide', 'classify', 'evaluate', 'judge', 'plan', 'handover',
    'run_review', 'clock_review', 'can_settle_first', 'pending_orders',
    'apply_corrections', 'scenario', 'attach', 'cross_berth_pairs',
    'tier_counts', 'correction_list',
]
