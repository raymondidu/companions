#!/usr/bin/env python3
"""Judge companion health from a reading AND the reading before it.

WHY THIS EXISTS

On 2026-09-11 the companion analyzer generated 56 signals and opened none
of them, for twelve hours, while every hourly check reported healthy.

Nothing was wrong with any single reading. opened_signals read 29 at
11:41, 12:25, 13:41, 14:41, 15:40, 17:24 and 17:38 -- and 29 is a
perfectly reasonable number. The fault was only ever visible in the
sequence. A snapshot cannot see a frozen counter; only a comparison can.

So this asserts DELTAS, not values.

TWO RULES, AND THE SECOND ONE MATTERS AS MUCH AS THE FIRST

1. Alarm on edges, report on levels. A fault is raised the pass it
   APPEARS. While it persists it is carried in the state file with a
   duration, and it does not re-alarm. The Gold watchdog workflow spent
   21 hours red on the same condition and everyone stopped reading it,
   which is how a real fault stayed invisible. An alarm that can be red
   for 21 hours is not an alarm.

2. Absence of evidence degrades. A missing field is never read as
   healthy. If we cannot tell, we say we cannot tell.

This module is pure: it takes two dicts and returns a verdict. It opens
no sockets and reads no files, so every branch below is unit-tested
against the real production shapes rather than exercised in anger.
"""
from __future__ import annotations

import json
import sys
from typing import Any


# The live contract. Drift here is never acceptable and never rate-limited:
# it is the one condition that alarms on every pass, not just its first.
EXPECTED_CONTRACT = {
    'cohort': 'EXNESS_SURVIVAL_V1',
    'active_paths': {'USOIL': 'BALANCED_CLEAN', 'BTC': 'GOLD_M30_LOCAL_STRUCTURE'},
}
SILVER_MUST_BE = 'REFUSED_INSTRUMENT'

# Raymond's explicit escalation trigger. Movement off this value is the
# only open-failure count that is ours to escalate; BROKER_OPEN_FAILED and
# BELOW_MIN_CAPITAL are ruled owner-side and are reported, never raised.
UNSPECIFIED_PINNED_AT = 129

HEARTBEAT_MAX_SECONDS = 120.0

# One interval of no opening can be timing. Three consecutive intervals in
# which delivery advanced and opening did not is a stalled pipeline.
OPENING_STALL_INTERVALS = 3


def _num(value, default=None):
    """Absence of evidence degrades: an unreadable number is None, never 0."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


def extract(health: dict[str, Any]) -> dict[str, Any]:
    """Shape the dashboard's /health payload into what assess() compares.

    Deliberately the only place that knows the health schema. The remote
    host does nothing but curl and print, so a change in that payload
    breaks a unit test here rather than a live SSH heredoc -- a badly
    indented heredoc silently killed an entire in-container read once
    already, and the workflow reported success while doing so.

    Every lookup degrades to None rather than to a default, because a
    field we cannot read must never arrive at assess() looking healthy.
    """
    if not isinstance(health, dict):
        return {}
    th = health.get('tradehouse')
    th = th if isinstance(th, dict) else {}
    summary = th.get('summary')
    summary = summary if isinstance(summary, dict) else {}
    raw_markets = health.get('markets')
    raw_markets = raw_markets if isinstance(raw_markets, dict) else {}

    markets, scans = {}, []
    for name, m in raw_markets.items():
        if not isinstance(m, dict):
            markets[name] = None
            continue
        markets[name] = {
            'state': m.get('state'),
            'live_gate': m.get('live_signal_gate'),
            'heartbeat_age_s': m.get('heartbeat_age_seconds'),
        }
        c = _num(m.get('scan_count'))
        if c is not None:
            scans.append(c)

    out = {'markets': markets, 'scans': max(scans) if scans else None}
    if 'tradehouse' in health:
        out['contract'] = {'cohort': th.get('cohort'),
                           'active_paths': th.get('active_paths')}
        out['open_failure_reasons'] = th.get('open_failure_reasons') or {}
        out['lifecycle'] = {k: summary.get(k) for k in (
            'generated', 'sent', 'accepted', 'opened_signals', 'opened_positions',
            'closed_signals', 'closed_positions',
            'open_failed_signals', 'open_failed_positions')}
    return out


def assess(current: dict[str, Any],
           previous: dict[str, Any] | None,
           now_epoch: float) -> dict[str, Any]:
    """Compare a reading to the one before it.

    Returns {'faults': [...new this pass...], 'ongoing': [...carried...],
             'state': {...persist this...}}
    """
    prev = previous or {}
    prev_open = {f['code']: f for f in prev.get('open_faults', [])}
    found: dict[str, dict] = {}

    def raise_fault(code, detail, always=False):
        found[code] = {
            'code': code,
            'detail': detail,
            'since': prev_open.get(code, {}).get('since', now_epoch),
            'always_alarms': always,
        }

    # ---- 1. contract drift -------------------------------------------
    contract = current.get('contract')
    if not isinstance(contract, dict):
        raise_fault('CONTRACT_UNREADABLE',
                    'no contract block in telemetry; cannot confirm the live contract',
                    always=True)
    else:
        if contract.get('cohort') != EXPECTED_CONTRACT['cohort']:
            raise_fault('CONTRACT_DRIFT',
                        'cohort is %r, expected %r' % (contract.get('cohort'),
                                                       EXPECTED_CONTRACT['cohort']),
                        always=True)
        paths = contract.get('active_paths')
        if paths != EXPECTED_CONTRACT['active_paths']:
            raise_fault('CONTRACT_DRIFT_PATHS',
                        'active_paths are %r, expected %r' % (
                            paths, EXPECTED_CONTRACT['active_paths']),
                        always=True)

    # ---- 2. markets alive --------------------------------------------
    markets = current.get('markets')
    if not isinstance(markets, dict) or not markets:
        raise_fault('MARKETS_UNREADABLE', 'no market block in telemetry', always=True)
    else:
        for name, m in sorted(markets.items()):
            if not isinstance(m, dict):
                raise_fault('MARKET_UNREADABLE_%s' % name, 'market %s unreadable' % name)
                continue
            if m.get('state') != 'RUNNING':
                raise_fault('MARKET_NOT_RUNNING_%s' % name,
                            '%s state is %r' % (name, m.get('state')))
            age = _num(m.get('heartbeat_age_s'))
            if age is None:
                raise_fault('HEARTBEAT_UNREADABLE_%s' % name,
                            '%s has no readable heartbeat age' % name)
            elif age > HEARTBEAT_MAX_SECONDS:
                raise_fault('HEARTBEAT_STALE_%s' % name,
                            '%s heartbeat %.0fs old, limit %.0fs' % (
                                name, age, HEARTBEAT_MAX_SECONDS))
            if name == 'SILVER' and m.get('live_gate') != SILVER_MUST_BE:
                raise_fault('SILVER_NOT_REFUSED',
                            'SILVER gate is %r, must be %s' % (
                                m.get('live_gate'), SILVER_MUST_BE),
                            always=True)

    # ---- 3. the scanner is still scanning ------------------------------
    scans = _num(current.get('scans'))
    prev_scans = _num(prev.get('scans'))
    if scans is None:
        raise_fault('SCANS_UNREADABLE', 'scan count unreadable')
    elif prev_scans is not None and scans <= prev_scans:
        raise_fault('SCANS_NOT_ADVANCING',
                    'scans stuck at %s since the previous reading' % scans)

    # ---- 4. THE 2026-09-11 BUG: delivery advances, opening does not ----
    life = current.get('lifecycle') or {}
    sent = _num(life.get('sent'))
    opened = _num(life.get('opened_signals'))
    prev_life = prev.get('lifecycle') or {}
    prev_sent = _num(prev_life.get('sent'))
    prev_opened = _num(prev_life.get('opened_signals'))

    stall_intervals = _num(prev.get('opening_stall_intervals'), 0) or 0
    if None in (sent, opened):
        raise_fault('LIFECYCLE_UNREADABLE', 'sent or opened_signals unreadable')
    elif None not in (prev_sent, prev_opened):
        if sent > prev_sent and opened == prev_opened:
            stall_intervals += 1
        elif opened > prev_opened:
            stall_intervals = 0
        # sent flat too means nothing was offered; that is not a stall.
        if stall_intervals >= OPENING_STALL_INTERVALS:
            raise_fault('OPENING_STALLED',
                        'delivery advanced over %d consecutive readings while '
                        'opened_signals stayed at %s' % (stall_intervals, opened))

    # ---- 5. the one open-failure trigger that is ours ------------------
    reasons = current.get('open_failure_reasons')
    if not isinstance(reasons, dict):
        raise_fault('OPEN_FAILURE_REASONS_UNREADABLE', 'reason block unreadable')
    else:
        unspecified = _num(reasons.get('UNSPECIFIED_OPEN_FAILURE'))
        if unspecified is None:
            raise_fault('UNSPECIFIED_UNREADABLE',
                        'UNSPECIFIED_OPEN_FAILURE not present in the reason block')
        elif unspecified != UNSPECIFIED_PINNED_AT:
            raise_fault('UNSPECIFIED_MOVED',
                        'UNSPECIFIED_OPEN_FAILURE is %s, pinned at %s' % (
                            unspecified, UNSPECIFIED_PINNED_AT),
                        always=True)
        prev_reasons = prev.get('open_failure_reasons') or {}
        if prev_reasons:
            new_codes = sorted(set(reasons) - set(prev_reasons))
            if new_codes:
                raise_fault('NEW_FAILURE_REASON',
                            'reason codes not seen in the previous reading: %s'
                            % ', '.join(new_codes))

    # ---- verdict -------------------------------------------------------
    # Edge alarm: new this pass, or flagged always_alarms.
    faults = [f for f in found.values()
              if f['code'] not in prev_open or f['always_alarms']]
    ongoing = [f for f in found.values()
               if f['code'] in prev_open and not f['always_alarms']]
    for f in list(found.values()):
        f['open_for_seconds'] = round(now_epoch - f['since'], 1)

    cleared = sorted(set(prev_open) - set(found))

    state = {
        'captured_at_epoch': now_epoch,
        'scans': scans,
        'lifecycle': life,
        'open_failure_reasons': reasons if isinstance(reasons, dict) else {},
        'opening_stall_intervals': stall_intervals,
        'open_faults': sorted(found.values(), key=lambda f: f['code']),
        'cleared_this_pass': cleared,
    }
    return {'faults': sorted(faults, key=lambda f: f['code']),
            'ongoing': sorted(ongoing, key=lambda f: f['code']),
            'cleared': cleared,
            'state': state}


def main(argv):
    if len(argv) < 3:
        print('usage: companion_health_assert.py <current.json> <previous.json|NONE> '
              '<out_state.json>', file=sys.stderr)
        return 2
    import time
    current = extract(json.load(open(argv[0])))
    previous = None if argv[1] == 'NONE' else json.load(open(argv[1]))
    verdict = assess(current, previous, time.time())

    for f in verdict['faults']:
        print('COMPANION_FAULT_NEW %s %s' % (f['code'], f['detail']))
    for f in verdict['ongoing']:
        print('COMPANION_FAULT_ONGOING %s (%.0fs) %s' % (
            f['code'], f['open_for_seconds'], f['detail']))
    for c in verdict['cleared']:
        print('COMPANION_FAULT_CLEARED %s' % c)
    if not verdict['faults'] and not verdict['ongoing']:
        print('COMPANION_HEALTH_OK all delta assertions passed')

    json.dump(verdict['state'], open(argv[2], 'w'), indent=2, sort_keys=True)
    # Red only for NEW faults. Ongoing ones live in the state file so the
    # signal stays believable.
    return 1 if verdict['faults'] else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
