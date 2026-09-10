"""Guards on what may actually reach TradeHouse.

The pre-existing "Static live-authority guard" in CI asserts fields on the
hardcoded dict returned by isolation_contract(). Those fields are constants, so
the guard passes no matter what is routed live: adding SILVER to ACTIVE_PATHS
leaves it green. It reads like a safety check and enforces nothing.

These tests assert the things that actually decide live routing:

  * the live allowlist itself, so a fourth instrument cannot be added quietly
  * Silver's policy refusal, which is the single worst regression available
  * the LONG/SHORT wire contract, proven in production when TradeHouse rejected
    BUY with INVALID_DIRECTION and only defended by a comment until now
  * the absence of any sizing or capital instruction on the wire, since
    TradeHouse owns all of that
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

import companion_tradehouse as th
from companion_tradehouse import ACTIVE_PATHS, COHORT, EXECUTOR_INSTRUMENT

# The frozen live routing policy. Changing anything here is a live policy change
# and needs explicit human approval, not a test edit to make a red build green.
ALLOWLIST = {'USOIL': 'BALANCED_CLEAN', 'BTC': 'GOLD_M30_LOCAL_STRUCTURE'}
PAPER_ONLY_INSTRUMENTS = {'SILVER', 'XAGUSD', 'GOLD', 'XAUUSD', 'ETH', 'ETHUSD'}


def test_live_allowlist_is_exactly_oil_and_btc():
    assert ACTIVE_PATHS == ALLOWLIST, (
        'the live allowlist changed; only USOIL/BALANCED_CLEAN and '
        'BTC/GOLD_M30_LOCAL_STRUCTURE may reach TradeHouse'
    )
    assert COHORT == 'EXNESS_SURVIVAL_V1'
    assert set(EXECUTOR_INSTRUMENT) == set(ALLOWLIST)
    assert EXECUTOR_INSTRUMENT == {'USOIL': 'USOIL', 'BTC': 'BTCUSD'}


@pytest.mark.parametrize('market', sorted(PAPER_ONLY_INSTRUMENTS))
def test_paper_only_instruments_can_never_be_routed_live(market):
    """Silver especially: REFUSED_INSTRUMENT is policy, not a prediction failure."""
    assert market not in ACTIVE_PATHS
    assert market not in EXECUTOR_INSTRUMENT


def test_silver_delivery_refuses_before_anything_else(tmp_path, monkeypatch):
    """Refusal must not depend on credentials, freshness or a candidate."""
    monkeypatch.setenv('COMPANION_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('COMPANION_EXECUTOR_BASE_URL', 'https://example.invalid')
    monkeypatch.setenv('COMPANION_EXECUTOR_SECRET', 'not-a-real-secret')

    result = asyncio.run(th.deliver_selected_signal(
        'SILVER',
        {'BALANCED_CLEAN': {'research_policy': {'cohort': COHORT}}},
        live_candidate={
            'direction': 'LONG', 'reference_price': 30.0,
            'created_at': th._utcnow(), 'setup': 'TREND_CONTINUATION',
        },
        setup_key='silver-should-never-send',
        live_gate='ACCEPTED',
    ))

    assert result['status'] == 'REFUSED_INSTRUMENT'
    assert result['sent'] is False
    assert result['eligible'] is False


def test_wire_direction_is_long_short_never_buy_sell():
    """TradeHouse rejected BUY with INVALID_DIRECTION. Keep the contract."""
    source = inspect.getsource(th.deliver_selected_signal)
    assert "internal_direction not in {\"LONG\", \"SHORT\"}" in source.replace("'", '"'), source
    assert 'executor_direction = internal_direction' in source
    # No translation table may map an internal direction onto BUY/SELL.
    for forbidden in ("'BUY'", '"BUY"', "'SELL'", '"SELL"'):
        assert forbidden not in source, f'{forbidden} must never appear on the wire path'


@pytest.mark.parametrize('bad_direction', ['BUY', 'SELL', 'buy', 'sell', 'LONG_ENTRY', ''])
def test_invalid_direction_fails_closed(tmp_path, monkeypatch, bad_direction):
    monkeypatch.setenv('COMPANION_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('COMPANION_EXECUTOR_BASE_URL', 'https://example.invalid')
    monkeypatch.setenv('COMPANION_EXECUTOR_SECRET', 'not-a-real-secret')

    result = asyncio.run(th.deliver_selected_signal(
        'USOIL',
        {'BALANCED_CLEAN': {'research_policy': {'cohort': COHORT}}},
        live_candidate={
            'direction': bad_direction, 'reference_price': 100.0,
            'created_at': th._utcnow(), 'setup': 'TREND_CONTINUATION',
        },
        setup_key='bad-direction',
        live_gate='ACCEPTED',
    ))

    assert result['status'] == 'INVALID_DIRECTION'
    assert result['sent'] is False


def test_wire_payload_carries_no_capital_or_sizing_instruction():
    """Companion supplies direction and reference price. TradeHouse owns money."""
    source = inspect.getsource(th.deliver_selected_signal)
    payload_block = source.split('payload = {', 1)[1].split('}', 1)[0]
    for banned in (
        'lot', 'capital', 'wallet', 'leverage', 'margin',
        'stop_loss', 'take_profit', 'sl', 'tp', 'size', 'volume', 'risk',
    ):
        assert banned not in payload_block.lower(), (
            f'{banned!r} appears in the TradeHouse payload; sizing and capital '
            f'belong to TradeHouse, never to Companion'
        )


def test_unconfigured_executor_fails_closed_rather_than_sending(tmp_path, monkeypatch):
    monkeypatch.setenv('COMPANION_DATA_DIR', str(tmp_path))
    for name in (
        'COMPANION_EXECUTOR_BASE_URL', 'TRADEHOUSE_EXECUTOR_BASE_URL',
        'GOLD_EXECUTOR_BASE_URL', 'EXECUTOR_BASE_URL',
        'COMPANION_EXECUTOR_SECRET', 'TRADEHOUSE_EXECUTOR_SECRET',
        'GOLD_EXECUTOR_SECRET', 'EXECUTOR_SECRET',
    ):
        monkeypatch.delenv(name, raising=False)

    result = asyncio.run(th.deliver_selected_signal(
        'BTC',
        {'GOLD_M30_LOCAL_STRUCTURE': {'research_policy': {'cohort': COHORT}}},
        live_candidate={
            'direction': 'SHORT', 'reference_price': 60000.0,
            'created_at': th._utcnow(), 'setup': 'M30_LOCAL_STRUCTURE',
        },
        setup_key='no-credentials',
        live_gate='ACCEPTED',
    ))

    assert result['status'] == 'EXECUTOR_UNCONFIGURED'
    assert result['sent'] is False


def test_wrong_cohort_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv('COMPANION_DATA_DIR', str(tmp_path))
    result = asyncio.run(th.deliver_selected_signal(
        'USOIL',
        {'BALANCED_CLEAN': {'research_policy': {'cohort': 'SOME_OTHER_COHORT'}}},
        live_candidate={
            'direction': 'LONG', 'reference_price': 100.0,
            'created_at': th._utcnow(), 'setup': 'TREND_CONTINUATION',
        },
        setup_key='wrong-cohort',
        live_gate='ACCEPTED',
    ))
    assert result['status'] == 'WRONG_COHORT'
    assert result['sent'] is False


def test_stale_signal_is_never_sent_as_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv('COMPANION_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('COMPANION_EXECUTOR_BASE_URL', 'https://example.invalid')
    monkeypatch.setenv('COMPANION_EXECUTOR_SECRET', 'not-a-real-secret')

    from datetime import datetime, timedelta, timezone
    stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()

    result = asyncio.run(th.deliver_selected_signal(
        'USOIL',
        {'BALANCED_CLEAN': {'research_policy': {'cohort': COHORT}}},
        live_candidate={
            'direction': 'LONG', 'reference_price': 100.0,
            'created_at': stale, 'setup': 'TREND_CONTINUATION',
        },
        setup_key='stale',
        live_gate='ACCEPTED',
    ))
    assert result['status'] == 'STALE_SIGNAL'
    assert result['sent'] is False
