"""Signals and positions must never be counted in the same breath.

One Companion signal fans out to many funded TradeHouse accounts. So a count of
signals and a count of positions are different units, and mixing them produced a
lifecycle row where "opened" could read higher than "accepted", which looks
impossible and quietly undermines every number beside it.

TradeHouse's "opened for N accounts" is a position count. Distinct predictions
that opened anywhere is a signal count. Both are published; neither may drift
into the other.
"""
from __future__ import annotations

import json

import pytest

import companion_tradehouse as th


def _seed(tmp_path, monkeypatch, positions):
    """positions: {signal_id: [(tradehouse_id, opened: bool, closed: bool, failed: bool)]}"""
    monkeypatch.setenv('COMPANION_DATA_DIR', str(tmp_path))

    signals = {}
    callbacks = {'signals': {}}
    for signal_id, legs in positions.items():
        signals[signal_id] = {
            'signal_id': signal_id, 'attempted_at': '2026-09-10T00:00:00+00:00',
            'http_status': 200, 'ack': {'accepted': True},
        }
        rows = {}
        for tradehouse_id, opened, closed, failed in legs:
            row = {'tradehouse_id': tradehouse_id, 'signal_id': signal_id}
            if opened:
                row['broker_position_id'] = f'BRK-{tradehouse_id}'
            if closed:
                row['lifecycle_state'] = 'CLOSED'
            if failed:
                row['last_event'] = 'OPEN_FAILED'
                row['reason_code'] = 'BROKER_OPEN_FAILED'
            rows[tradehouse_id] = row
        callbacks['signals'][signal_id] = {'positions': rows}

    (tmp_path / 'tradehouse_delivery.json').write_text(json.dumps({'signals': signals}))
    (tmp_path / 'tradehouse_callbacks.json').write_text(json.dumps(callbacks))


def test_one_signal_fanned_out_counts_once_as_a_signal_and_many_as_positions(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, {
        'OIL-aaa': [(f'TH-{i}', True, False, False) for i in range(20)],
    })
    s = th.delivery_snapshot()['summary']

    assert s['generated'] == 1
    assert s['accepted'] == 1
    assert s['opened_signals'] == 1, 'one prediction opened, however many accounts took it'
    assert s['opened_positions'] == 20, 'twenty accounts opened'


def test_failed_signals_and_failed_account_opens_stay_distinct(tmp_path, monkeypatch):
    """The charter's worked example: 1 signal, 20 accounts, 10 account failures."""
    legs = [(f'TH-{i}', True, False, False) for i in range(10)]
    legs += [(f'TH-{i}', False, False, True) for i in range(10, 20)]
    _seed(tmp_path, monkeypatch, {'BTC-bbb': legs})

    s = th.delivery_snapshot()['summary']

    assert s['open_failed_signals'] == 1, 'one prediction signal, not ten'
    assert s['open_failed_positions'] == 10, 'ten account opens failed'
    assert s['opened_signals'] == 1
    assert s['opened_positions'] == 10


def test_position_counts_may_exceed_signal_counts_without_being_wrong(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, {
        'OIL-aaa': [(f'A-{i}', True, True, False) for i in range(12)],
        'BTC-bbb': [(f'B-{i}', True, False, False) for i in range(9)],
    })
    s = th.delivery_snapshot()['summary']

    assert s['accepted'] == 2
    assert s['opened_positions'] == 21
    assert s['opened_positions'] > s['accepted'], (
        'positions exceeding signals is expected under fan-out and must not be '
        'treated as a defect'
    )
    assert s['opened_signals'] == 2
    assert s['opened_signals'] <= s['accepted'], (
        'a signal can never open without having been accepted'
    )
    assert s['closed_signals'] == 1
    assert s['closed_positions'] == 12


def test_every_published_count_declares_its_basis(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, {'OIL-aaa': [('TH-1', True, False, False)]})
    s = th.delivery_snapshot()['summary']

    basis = s['counting_basis']
    for key in basis['signals'] + basis['positions']:
        assert key in s, f'{key} is declared in counting_basis but not published'
    assert not set(basis['signals']) & set(basis['positions']), (
        'a key cannot be both a signal count and a position count'
    )


@pytest.mark.parametrize('legacy_key', ['generated', 'sent', 'accepted', 'opened', 'closed',
                                        'open_failed', 'open_failed_signals', 'open_failed_positions'])
def test_existing_keys_are_preserved_for_anything_already_reading_them(tmp_path, monkeypatch, legacy_key):
    _seed(tmp_path, monkeypatch, {'OIL-aaa': [('TH-1', True, False, False)]})
    assert legacy_key in th.delivery_snapshot()['summary']


def test_empty_state_reports_zeroes_not_errors(tmp_path, monkeypatch):
    monkeypatch.setenv('COMPANION_DATA_DIR', str(tmp_path))
    s = th.delivery_snapshot()['summary']
    for key in ('generated', 'sent', 'accepted', 'opened_signals', 'opened_positions',
                'closed_signals', 'closed_positions'):
        assert s[key] == 0
