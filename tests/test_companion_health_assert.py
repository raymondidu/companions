"""The check that would have caught 2026-09-11.

That night the analyzer generated 56 signals and opened none of them for
twelve hours, and every hourly check reported healthy, because every
check was a snapshot and the fault was a trend.

These tests are built on the real telemetry shape read from production
at 17:38 UTC that day.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'companion_health_assert', ROOT / '.github' / 'scripts' / 'companion_health_assert.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assess = mod.assess

NOW = 1_789_150_000.0


def reading(**over):
    """The real 17:38 production reading."""
    base = {
        'contract': {
            'cohort': 'EXNESS_SURVIVAL_V1',
            'active_paths': {'USOIL': 'BALANCED_CLEAN', 'BTC': 'GOLD_M30_LOCAL_STRUCTURE'},
        },
        'scans': 1079,
        'lifecycle': {
            'generated': 56, 'sent': 54, 'accepted': 38, 'opened_signals': 29,
            'opened_positions': 47, 'closed_signals': 20, 'closed_positions': 27,
            'open_failed_signals': 39, 'open_failed_positions': 711,
        },
        'open_failure_reasons': {
            'BROKER_OPEN_FAILED': 505, 'UNSPECIFIED_OPEN_FAILURE': 129,
            'BELOW_MIN_CAPITAL': 45, 'STALE_SIGNAL': 30,
            'INSTRUMENT_POSITION_OPEN': 1, 'NO_BROKER': 1,
        },
        'markets': {
            'USOIL': {'state': 'RUNNING', 'live_gate': 'VOLUME_BELOW_0_85X', 'heartbeat_age_s': 39.0},
            'BTC': {'state': 'RUNNING', 'live_gate': 'M30_LOCAL_STRUCTURE_BREAK_NOT_CONFIRMED', 'heartbeat_age_s': 36.9},
            'SILVER': {'state': 'RUNNING', 'live_gate': 'REFUSED_INSTRUMENT', 'heartbeat_age_s': 38.4},
        },
    }
    base.update(over)
    return base


def codes(verdict, key='faults'):
    return [f['code'] for f in verdict[key]]


# ---------------------------------------------------------------- healthy

def test_a_healthy_pair_of_readings_is_silent():
    prev = assess(reading(), None, NOW - 900)['state']
    v = assess(reading(scans=1100, lifecycle=dict(reading()['lifecycle'], sent=55, opened_signals=30)),
               prev, NOW)
    assert codes(v) == [] and codes(v, 'ongoing') == []


def test_first_ever_reading_does_not_invent_deltas():
    """With no previous reading there is nothing to compare; it must not
    guess, and must not alarm on the absence."""
    v = assess(reading(), None, NOW)
    assert 'SCANS_NOT_ADVANCING' not in codes(v)
    assert 'OPENING_STALLED' not in codes(v)


# ------------------------------------------------- the bug it exists for

def test_opening_stall_is_caught_on_the_third_interval():
    """Delivery advances, opening does not. This is 2026-09-11 exactly."""
    state = assess(reading(), None, NOW - 3600)['state']
    sent = 54
    for i in range(1, 4):
        sent += 1
        cur = reading(scans=1079 + i * 20,
                      lifecycle=dict(reading()['lifecycle'], sent=sent, opened_signals=29))
        v = assess(cur, state, NOW - 3600 + i * 900)
        state = v['state']
        if i < 3:
            assert 'OPENING_STALLED' not in codes(v), 'alarmed too early at interval %d' % i
    assert 'OPENING_STALLED' in codes(v)
    assert '29' in [f for f in v['faults'] if f['code'] == 'OPENING_STALLED'][0]['detail']


def test_a_single_quiet_interval_is_not_a_stall():
    prev = assess(reading(), None, NOW - 900)['state']
    v = assess(reading(lifecycle=dict(reading()['lifecycle'], sent=55)), prev, NOW)
    assert 'OPENING_STALLED' not in codes(v)


def test_an_open_resets_the_stall_counter():
    state = assess(reading(), None, NOW - 3600)['state']
    sent = 54
    for i in range(1, 3):
        sent += 1
        state = assess(reading(lifecycle=dict(reading()['lifecycle'], sent=sent, opened_signals=29)),
                       state, NOW - 3600 + i * 900)['state']
    assert state['opening_stall_intervals'] == 2
    after = assess(reading(lifecycle=dict(reading()['lifecycle'], sent=57, opened_signals=30)),
                   state, NOW)
    assert after['state']['opening_stall_intervals'] == 0


def test_delivery_flat_as_well_is_not_reported_as_a_stall():
    """Nothing offered is not the same as nothing opening."""
    state = assess(reading(), None, NOW - 3600)['state']
    for i in range(1, 5):
        state = assess(reading(scans=1079 + i * 20), state, NOW - 3600 + i * 900)['state']
    assert state['opening_stall_intervals'] == 0


# ------------------------------------------------- alarm on edges

def test_an_ordinary_fault_alarms_once_then_becomes_ongoing():
    """The 21-hour-red-alarm problem. A persisting fault must stop shouting."""
    broken = reading(markets=dict(reading()['markets'],
                                  BTC={'state': 'STOPPED', 'live_gate': 'x', 'heartbeat_age_s': 10}))
    first = assess(broken, assess(reading(), None, NOW - 1800)['state'], NOW - 900)
    assert 'MARKET_NOT_RUNNING_BTC' in codes(first)
    second = assess(broken, first['state'], NOW)
    assert 'MARKET_NOT_RUNNING_BTC' not in codes(second)
    assert 'MARKET_NOT_RUNNING_BTC' in codes(second, 'ongoing')
    assert second['ongoing'][0]['open_for_seconds'] >= 900


def test_contract_drift_alarms_every_single_pass():
    """Some things are never allowed to become background noise."""
    drifted = reading(contract={'cohort': 'SOMETHING_ELSE',
                                'active_paths': {'USOIL': 'BALANCED_CLEAN',
                                                 'BTC': 'GOLD_M30_LOCAL_STRUCTURE'}})
    first = assess(drifted, assess(reading(), None, NOW - 1800)['state'], NOW - 900)
    assert 'CONTRACT_DRIFT' in codes(first)
    second = assess(drifted, first['state'], NOW)
    assert 'CONTRACT_DRIFT' in codes(second), 'contract drift went quiet on the second pass'


def test_unspecified_moving_off_129_alarms_every_pass():
    moved = reading(open_failure_reasons=dict(reading()['open_failure_reasons'],
                                              UNSPECIFIED_OPEN_FAILURE=130))
    first = assess(moved, assess(reading(), None, NOW - 1800)['state'], NOW - 900)
    assert 'UNSPECIFIED_MOVED' in codes(first)
    assert 'UNSPECIFIED_MOVED' in codes(assess(moved, first['state'], NOW))


def test_a_cleared_fault_is_reported_as_cleared():
    broken = reading(markets=dict(reading()['markets'],
                                  BTC={'state': 'STOPPED', 'live_gate': 'x', 'heartbeat_age_s': 10}))
    s = assess(broken, assess(reading(), None, NOW - 1800)['state'], NOW - 900)['state']
    v = assess(reading(scans=1100), s, NOW)
    assert 'MARKET_NOT_RUNNING_BTC' in v['cleared']


# ------------------------------------------------- the other invariants

def test_a_stopped_scanner_is_caught():
    prev = assess(reading(), None, NOW - 900)['state']
    assert 'SCANS_NOT_ADVANCING' in codes(assess(reading(), prev, NOW))


def test_a_stale_heartbeat_is_caught():
    stale = reading(markets=dict(reading()['markets'],
                                 USOIL={'state': 'RUNNING', 'live_gate': 'x', 'heartbeat_age_s': 400}))
    assert 'HEARTBEAT_STALE_USOIL' in codes(assess(stale, None, NOW))


def test_silver_must_stay_refused():
    s = reading(markets=dict(reading()['markets'],
                             SILVER={'state': 'RUNNING', 'live_gate': 'ACCEPTED', 'heartbeat_age_s': 20}))
    assert 'SILVER_NOT_REFUSED' in codes(assess(s, None, NOW))


def test_a_brand_new_failure_reason_is_surfaced():
    prev = assess(reading(), None, NOW - 900)['state']
    cur = reading(scans=1100, open_failure_reasons=dict(reading()['open_failure_reasons'],
                                                        MARGIN_EXHAUSTED=3))
    v = assess(cur, prev, NOW)
    assert 'NEW_FAILURE_REASON' in codes(v)
    assert 'MARGIN_EXHAUSTED' in [f for f in v['faults'] if f['code'] == 'NEW_FAILURE_REASON'][0]['detail']


# ------------------------------------------------- absence degrades

@pytest.mark.parametrize('drop,expected', [
    ('contract', 'CONTRACT_UNREADABLE'),
    ('markets', 'MARKETS_UNREADABLE'),
    ('open_failure_reasons', 'OPEN_FAILURE_REASONS_UNREADABLE'),
])
def test_a_missing_block_is_never_read_as_healthy(drop, expected):
    cur = reading()
    cur.pop(drop)
    assert expected in codes(assess(cur, None, NOW))


def test_an_unreadable_lifecycle_is_never_read_as_healthy():
    assert 'LIFECYCLE_UNREADABLE' in codes(assess(reading(lifecycle={}), None, NOW))


def test_zero_is_not_confused_with_missing():
    """_num must not turn a real 0 into 'unreadable', nor None into 0."""
    assert mod._num(0) == 0
    assert mod._num(None) is None
    assert mod._num('nonsense') is None
    assert mod._num(True) is None, 'a bool is not a count'


# ------------------------------------------------- the health-payload shim

def health_payload(**over):
    """The dashboard /health shape the remote host actually returns."""
    base = {
        'ok': True, 'deploy_sha': 'db5f2fe4',
        'tradehouse': {
            'cohort': 'EXNESS_SURVIVAL_V1',
            'active_paths': {'USOIL': 'BALANCED_CLEAN', 'BTC': 'GOLD_M30_LOCAL_STRUCTURE'},
            'open_failure_reasons': {
                'BROKER_OPEN_FAILED': 505, 'UNSPECIFIED_OPEN_FAILURE': 129,
                'BELOW_MIN_CAPITAL': 45, 'STALE_SIGNAL': 30,
                'INSTRUMENT_POSITION_OPEN': 1, 'NO_BROKER': 1},
            'summary': {'generated': 56, 'sent': 54, 'accepted': 38,
                        'opened_signals': 29, 'opened_positions': 47,
                        'closed_signals': 20, 'closed_positions': 27,
                        'open_failed_signals': 39, 'open_failed_positions': 711},
        },
        'markets': {
            'USOIL': {'state': 'RUNNING', 'live_signal_gate': 'VOLUME_BELOW_0_85X',
                      'heartbeat_age_seconds': 39.0, 'scan_count': 1079},
            'BTC': {'state': 'RUNNING', 'live_signal_gate': 'M30_LOCAL_STRUCTURE_BREAK_NOT_CONFIRMED',
                    'heartbeat_age_seconds': 36.9, 'scan_count': 1079},
            'SILVER': {'state': 'RUNNING', 'live_signal_gate': 'REFUSED_INSTRUMENT',
                       'heartbeat_age_seconds': 38.4, 'scan_count': 1079},
        },
    }
    base.update(over)
    return base


def test_extract_reproduces_the_1738_reading_exactly():
    got = mod.extract(health_payload())
    assert got['scans'] == 1079
    assert got['contract'] == {
        'cohort': 'EXNESS_SURVIVAL_V1',
        'active_paths': {'USOIL': 'BALANCED_CLEAN', 'BTC': 'GOLD_M30_LOCAL_STRUCTURE'}}
    assert got['lifecycle']['opened_signals'] == 29
    assert got['lifecycle']['open_failed_positions'] == 711
    assert got['open_failure_reasons']['UNSPECIFIED_OPEN_FAILURE'] == 129
    assert got['markets']['SILVER']['live_gate'] == 'REFUSED_INSTRUMENT'
    assert got['markets']['BTC']['heartbeat_age_s'] == 36.9


def test_extracted_healthy_payload_passes_assess():
    prev = assess(mod.extract(health_payload()), None, NOW - 900)['state']
    later = health_payload()
    later['markets']['BTC']['scan_count'] = 1100
    later['tradehouse']['summary']['sent'] = 55
    later['tradehouse']['summary']['opened_signals'] = 30
    assert codes(assess(mod.extract(later), prev, NOW)) == []


def test_a_health_payload_missing_tradehouse_is_not_read_as_healthy():
    """If the block is gone we must not silently report a perfect contract."""
    p = health_payload(); p.pop('tradehouse')
    v = assess(mod.extract(p), None, NOW)
    assert 'CONTRACT_UNREADABLE' in codes(v)
    assert 'OPEN_FAILURE_REASONS_UNREADABLE' in codes(v)


def test_garbage_in_does_not_become_health():
    for junk in (None, [], 'not json', 0):
        assert mod.extract(junk) == {} or mod.extract(junk).get('markets') in (None, {})
    v = assess(mod.extract(None), None, NOW)
    assert 'MARKETS_UNREADABLE' in codes(v) and 'CONTRACT_UNREADABLE' in codes(v)
