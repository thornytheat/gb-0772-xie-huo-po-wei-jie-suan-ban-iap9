"""杆侧双校时：先结/待核边界、确认重算与旧校时重跑幂等。"""
from datetime import datetime

import pytest

from app.curb.calibrate import (REASON_BOOKING, REASON_OVERLAP, REASON_TIER,
                                assess)


def dt(text):
    return datetime.fromisoformat(text).astimezone()


def stay(key, berth, vehicle, start, end):
    return {'stay': key, 'berth': berth, 'vehicle': vehicle,
            'start': dt(start), 'end': dt(end)}


def booking(reservation, berth, vehicle, effective, start, end, state='ACTIVE'):
    return {'reservation': reservation, 'berth': berth, 'vehicle': vehicle,
            'effective': dt(effective), 'start': dt(start), 'end': dt(end),
            'state': state, 'line': 1}


def histories(*rows):
    bucket = {}
    for row in rows:
        bucket.setdefault(row['reservation'], []).append(row)
    return bucket


def test_equal_due_under_both_clocks_settles_first():
    stops = [stay('A', 'B-01', 'CAR-X', '2026-11-30T20:00+08:00', '2026-11-30T20:10+08:00')]
    result = assess(stops, {'A': 1})
    verdict = result['lines'][0]
    assert verdict['due_before_cents'] == verdict['due_after_cents'] == 200
    assert verdict['settleable'] and not verdict['hold']
    assert result['settle_count'] == 1 and result['hold_count'] == 0
    assert result['confirmable_due_cents'] == 200


def test_arrival_moved_before_cancel_holds_order():
    book = histories(
        booking('K1', 'B-01', 'CAR-C', '2026-11-30T19:00+08:00',
                '2026-11-30T20:00+08:00', '2026-11-30T21:00+08:00'),
        booking('K1', 'B-01', 'CAR-C', '2026-11-30T20:05+08:00',
                '2026-11-30T20:00+08:00', '2026-11-30T21:00+08:00', 'CANCEL'))
    stops = [stay('C', 'B-01', 'CAR-C', '2026-11-30T20:05+08:00', '2026-11-30T20:30+08:00')]
    verdict = assess(stops, {'C': -1}, histories=book)['lines'][0]
    assert (verdict['association_before'], verdict['association_after']) == ('CANCELLED', 'BOOKED')
    assert verdict['due_before_cents'] > verdict['due_after_cents']
    assert verdict['hold'] and 'DUE_CHANGED' in verdict['reasons']
    assert REASON_BOOKING in verdict['reasons']


def test_induced_cross_berth_overlap_holds_both_even_when_due_equal():
    book = histories(
        booking('KA', 'B-01', 'V', '2026-11-30T19:00+08:00',
                '2026-11-30T20:00+08:00', '2026-11-30T20:30+08:00'),
        booking('KB', 'B-02', 'V', '2026-11-30T19:00+08:00',
                '2026-11-30T20:00+08:00', '2026-11-30T20:30+08:00'))
    stops = [stay('O1', 'B-01', 'V', '2026-11-30T20:00+08:00', '2026-11-30T20:05+08:00'),
             stay('O2', 'B-02', 'V', '2026-11-30T20:05+08:00', '2026-11-30T20:10+08:00'),
             stay('O3', 'B-03', 'Z', '2026-11-30T21:00+08:00', '2026-11-30T21:10+08:00')]
    by_id = {v['stay']: v for v in assess(stops, {'O2': -1}, histories=book)['lines']}
    assert by_id['O1']['due_same'] and by_id['O2']['due_same']
    assert REASON_OVERLAP in by_id['O1']['reasons'] and REASON_OVERLAP in by_id['O2']['reasons']
    assert by_id['O1']['hold'] and by_id['O2']['hold'] and not by_id['O3']['hold']
    assert by_id['O1']['overlap_with'] == ['O2']
    assert set(assess(stops, {'O2': -1}, histories=book)['hold_ids']) == {'O1', 'O2'}


def test_time_delta_pushing_later_stop_over_tier_holds_that_order():
    stops = [stay('E', 'B-01', 'T', '2026-11-30T20:00+08:00', '2026-11-30T20:14+08:00'),
             stay('L', 'B-02', 'T', '2026-11-30T20:30+08:00', '2026-11-30T20:40+08:00')]
    by_id = {v['stay']: v for v in assess(
        stops, {'E': {'arrival_shift_minutes': -1}})['lines']}
    assert (by_id['L']['tier_before'], by_id['L']['tier_after']) == (
        {'BASE': 1, 'UPGRADE': 9}, {'BASE': 0, 'UPGRADE': 10})
    assert by_id['L']['hold'] and REASON_TIER in by_id['L']['reasons']
    assert by_id['E']['hold']


def test_zero_correction_matches_settlement_totals(dirty):
    from app.curb.bookings import revisions
    hist, _ = revisions(dirty.anonymous['bookings'][0], dirty.survey)
    result = assess(dirty.occupancy['stays'], histories=hist)
    assert result['due_before_cents'] == 2240
    assert all(v['due_same'] and v['settleable'] for v in result['lines'])


@pytest.fixture
def key(client):
    return client.post('/api/dockets', json={'sample': 'dirty'}).json()['key']


@pytest.fixture
def target(client):
    return client.get('/api/cases/dirty/occupancy').json()['stays'][1]['stay']


def test_review_endpoint_lists_settle_and_hold(client, target):
    response = client.post('/api/calibrations/review',
                           json={'sample': 'dirty',
                                 'corrections': [{'stay': target, 'shift_minutes': -2}]})
    assert response.status_code == 200
    body = response.json()
    assert body['total'] == 6 and body['hold_count'] >= 1
    assert len(body['settle_now']) + len(body['hold']) == 6


def test_confirmed_clock_exposes_recalculated_due_on_settlement(client, key, target):
    applied = client.post(f'/api/dockets/{key}/clocks',
                          json={'seq': 2, 'corrections': [{'stay': target, 'shift_minutes': -2}]})
    assert applied.json()['changed'] is True
    confirmed = client.post(f'/api/dockets/{key}/clocks/confirm', json={'seq': 2})
    assert confirmed.status_code == 200 and confirmed.json()['status'] == 'CONFIRMED'
    recalc = client.get(f'/api/dockets/{key}/recalculation').json()
    assert recalc['confirmed'] is True
    assert recalc['recalculation']['money']['due_cents'] == 2260
    page = client.get('/clearing/settlement', params={'key': key}).text
    assert '校时确认后的重算应收' in page and '22.60' in page


def test_rerunning_old_clock_after_new_keeps_current_conclusion(client, key, target):
    client.post(f'/api/dockets/{key}/clocks',
                json={'seq': 2, 'corrections': [{'stay': target, 'shift_minutes': -2}]})
    client.post(f'/api/dockets/{key}/clocks/confirm', json={'seq': 2})
    old = client.post(f'/api/dockets/{key}/clocks',
                      json={'seq': 1, 'corrections': [{'stay': target, 'shift_minutes': 99}]})
    assert old.json()['stale'] is True
    again = client.post(f'/api/dockets/{key}/clocks/confirm', json={'seq': 1})
    assert again.json().get('stale') is True
    recalc = client.get(f'/api/dockets/{key}/recalculation').json()
    assert recalc['recalculation']['money']['due_cents'] == 2260


def test_same_clock_event_is_idempotent_replay(client, key, target):
    event = {'seq': 3, 'id': 'T-3', 'corrections': [{'stay': target, 'shift_minutes': -2}]}
    first = client.post(f'/api/dockets/{key}/calibrations', json=event).json()
    second = client.post(f'/api/dockets/{key}/calibrations', json=event).json()
    assert first['status'] == 'APPLIED' and second['status'] == 'DUPLICATE'
    state = client.get(f'/api/dockets/{key}/calibrations').json()
    assert state['current_seq'] == 3
