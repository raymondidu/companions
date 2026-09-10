"""Scanner status truth: the H1/H4 candle counts the dashboard reads must stay
correct once counterfactual gates start emitting horizon observations.

Regression guard for a name collision in scan_one, where the per-gate horizon
dicts were bound to h1/h4 and clobbered the H1/H4 candle frames, so
out['data'] reported 3 bars instead of the real count. That produced a false
"missing M15/H1/H4 data" alarm on exactly the live BTC and Oil gates that
accumulate the most shadows.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pandas as pd
import pytest

import companion_runner as runner

BARS = 300
GATE = 'M30_LOCAL_STRUCTURE_BREAK_NOT_CONFIRMED'


def _bars(n: int, start: float, step: float) -> pd.DataFrame:
    rows = []
    price = start
    for i in range(n):
        open_, close = price, price + step
        rows.append({
            'time': f'2026-09-10T{i // 60:02d}:{i % 60:02d}:00Z',
            'open': open_, 'close': close,
            'high': max(open_, close) + abs(step) * 0.4,
            'low': min(open_, close) - abs(step) * 0.4,
            'volume': 1000.0 + i,
        })
        price = close
    return pd.DataFrame(rows)


class _StubData:
    async def candles(self, timeframe: str, count: int) -> pd.DataFrame:
        return {
            'M15': _bars(BARS, 60000, 3.0),
            'H1': _bars(BARS, 60000, 6.0),
            'H4': _bars(BARS, 60000, 9.0),
        }[timeframe]

    async def quote(self):
        last = 60000 + BARS * 3.0
        return SimpleNamespace(bid=last - 1.0, ask=last + 1.0, time='2026-09-10T05:00:00Z')


def _seed_shadows(data_dir, market: str, count: int) -> None:
    """Tracked shadows with recorded 1h/4h horizons, above the >=10 report threshold."""
    rows = [{
        'shadow_key': f'{market}|seed{i}|{GATE}', 'market': market, 'gate': GATE,
        'regime': 'SHORT_OK_LONG_PAUSE', 'direction': 'SHORT', 'entry': 60000.0,
        'created_at': '2026-09-09T00:00:00+00:00', 'created_scan': i, 'status': 'OPEN',
        'age_scans': 300, 'last_price': 60000.0, 'last_capital_return_pct': 1.0,
        'last_pnl_usd': 1.0, 'max_favorable_capital_pct': 8.0,
        'max_adverse_capital_pct': -1.5, 'reached_15pct_trigger': False,
        'reached_minus10pct_adverse': False,
        'partial_favorable_levels': {'5': '2026-09-09T01:00:00+00:00'},
        'horizon_outcomes': {
            '1h': {'capital_return_pct': 2.3, 'pnl_usd': 4.6, 'at': '2026-09-09T01:00:00+00:00'},
            '4h': {'capital_return_pct': 4.2, 'pnl_usd': 8.4, 'at': '2026-09-09T04:00:00+00:00'},
        },
        'sizing': {'paper_lot_size': None, 'contract_size': None,
                   'starting_equity_usd': 1000.0, 'paper_trade_usd': 200.0, 'leverage': 10.0},
    } for i in range(count)]
    path = data_dir / 'learning' / f'{market.lower()}_counterfactual.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, sort_keys=True, default=str))


@pytest.fixture()
def scanner(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(runner, '_provider', lambda key, symbol: (_StubData(), 'STUB'))

    async def _breadth(self):
        return {
            'state': 'SHORT_OK_LONG_PAUSE', 'pressure_score': 20,
            'long_allowed': False, 'short_allowed': True,
            'breadth_up_pct': 30, 'breadth_down_pct': 70,
            'volume_up_pct': 40, 'volume_down_pct': 60,
            'major_up': 1, 'major_down': 3,
        }

    monkeypatch.setattr(runner.BinanceBreadthData, 'snapshot', _breadth)
    return tmp_path


@pytest.mark.parametrize('tracked', [3, 12, 40])
def test_candle_counts_survive_horizon_reporting(scanner, tracked):
    """Candle counts stay truthful whether or not gates emit horizon observations."""
    _seed_shadows(scanner, 'BTC', tracked)
    runner.SCAN_COUNTS['BTC'] = 0

    out = asyncio.run(runner.scan_one('BTC'))

    assert out['state'] == 'RUNNING', out.get('error')
    assert out['data'] == {'m15_candles': BARS, 'h1_candles': BARS, 'h4_candles': BARS}


def test_horizon_observation_is_still_emitted(scanner):
    """The fix must not silence the counterfactual horizon reporting it touched."""
    _seed_shadows(scanner, 'BTC', 12)
    runner.SCAN_COUNTS['BTC'] = 0

    out = asyncio.run(runner.scan_one('BTC'))
    observations = out['learning']['observations']

    horizon_lines = [x for x in observations if 'forward horizons' in x]
    assert horizon_lines, observations
    assert '1h: 12 samples' in horizon_lines[0]
    assert '4h: 12 samples' in horizon_lines[0]


def test_silver_never_reaches_tradehouse(scanner):
    """Silver stays a policy-gated paper instrument: refused, never delivered."""
    runner.SCAN_COUNTS['SILVER'] = 0

    out = asyncio.run(runner.scan_one('SILVER'))

    assert out['live_signal_gate'] == 'REFUSED_INSTRUMENT'
    assert out['tradehouse_delivery']['status'] == 'REFUSED_INSTRUMENT'
    assert out['tradehouse_delivery']['sent'] is False
