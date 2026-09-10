from __future__ import annotations

"""Counterfactual research for rejected Companion live-path setups.

Research-only by construction: this module has no TradeHouse dependency and cannot
send, promote, or alter a live signal. It asks one question: after a live gate
rejected an otherwise directional setup, did price action subsequently show that
the gate likely protected capital or suppressed a profitable opportunity?
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TRIGGER_PCT = 15.0
ADVERSE_WARNING_PCT = -10.0
MAX_HOLD_SCANS = 240  # ~4h at 60s scans; enough for forward opportunity study.
MAX_RECORDS = 2000


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _path(data_dir: Path, market: str) -> Path:
    return data_dir / 'learning' / f'{market.lower()}_counterfactual.json'


def _load(data_dir: Path, market: str) -> list[dict]:
    p = _path(data_dir, market)
    if not p.exists():
        return []
    try:
        v = json.loads(p.read_text(encoding='utf-8'))
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _save(data_dir: Path, market: str, rows: list[dict]) -> None:
    p = _path(data_dir, market)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(rows[-MAX_RECORDS:], sort_keys=True, default=str), encoding='utf-8')
    tmp.replace(p)


def _capital_return(direction: str, entry: float, price: float, sizing: dict) -> tuple[float, float]:
    """Return (capital_return_pct, pnl_usd) using the live champion's paper sizing."""
    if entry <= 0:
        return 0.0, 0.0
    sign = 1.0 if direction == 'LONG' else -1.0
    lot = _f(sizing.get('paper_lot_size'))
    contract = _f(sizing.get('contract_size'))
    equity = max(_f(sizing.get('starting_equity_usd'), 1000.0), 1e-9)
    paper_trade = max(_f(sizing.get('paper_trade_usd'), equity), 1e-9)
    leverage = max(_f(sizing.get('leverage'), 1.0), 0.0)
    if lot > 0 and contract > 0:
        pnl = sign * (price - entry) * contract * lot
        return 100.0 * pnl / equity, pnl
    price_return_pct = sign * 100.0 * (price / entry - 1.0)
    cap = price_return_pct * leverage
    return cap, paper_trade * cap / 100.0


def infer_direction(m15, h1, h4) -> str | None:
    """Use the same 2-of-3 EMA20/EMA50 directional structure used by tournament paths."""
    try:
        from companion_engine import enrich
        a15, a1, a4 = enrich(m15), enrich(h1), enrich(h4)
        rows = (a15.iloc[-1], a1.iloc[-1], a4.iloc[-1])
        longs = sum(bool(r.ema20 > r.ema50) for r in rows)
        shorts = sum(bool(r.ema20 < r.ema50) for r in rows)
        if longs >= 2:
            return 'LONG'
        if shorts >= 2:
            return 'SHORT'
    except Exception:
        return None
    return None


def update_counterfactuals(
    data_dir: Path,
    market: str,
    setup_key: str,
    live_gate: str,
    direction: str | None,
    bid: float,
    ask: float,
    champion_row: dict | None,
    scan_count: int,
    regime: str | None = None,
) -> dict:
    rows = _load(data_dir, market)
    now = _utcnow()
    sizing = dict(champion_row or {})

    # Mark all existing open shadows first using executable-side mark prices.
    for r in rows:
        if r.get('status') != 'OPEN':
            continue
        d = r.get('direction')
        price = float(bid if d == 'LONG' else ask)
        cap, pnl = _capital_return(str(d), _f(r.get('entry')), price, r.get('sizing') or {})
        r['last_price'] = price
        r['last_capital_return_pct'] = round(cap, 4)
        r['last_pnl_usd'] = round(pnl, 4)
        r['max_favorable_capital_pct'] = round(max(_f(r.get('max_favorable_capital_pct')), cap), 4)
        r['max_adverse_capital_pct'] = round(min(_f(r.get('max_adverse_capital_pct')), cap), 4)
        r['age_scans'] = int(r.get('age_scans') or 0) + 1
        if r['max_favorable_capital_pct'] >= TRIGGER_PCT:
            r['reached_15pct_trigger'] = True
            if not r.get('trigger_reached_at'):
                r['trigger_reached_at'] = now
        if r['max_adverse_capital_pct'] <= ADVERSE_WARNING_PCT:
            r['reached_minus10pct_adverse'] = True
            if not r.get('minus10_reached_at'):
                r['minus10_reached_at'] = now
        if r.get('reached_15pct_trigger'):
            r['status'] = 'RESOLVED_BLOCKED_WINNER'
            r['resolution'] = 'GATE_BLOCKED_15PCT_TRIGGER'
            r['resolved_at'] = now
        elif r.get('reached_minus10pct_adverse'):
            r['status'] = 'RESOLVED_PROTECTED_LOSER'
            r['resolution'] = 'GATE_PROTECTED_FROM_MINUS10PCT_ADVERSE'
            r['resolved_at'] = now
        elif r['age_scans'] >= MAX_HOLD_SCANS:
            r['status'] = 'RESOLVED_INCONCLUSIVE'
            r['resolution'] = 'HORIZON_EXPIRED_WITHOUT_15PCT_OR_MINUS10PCT'
            r['resolved_at'] = now

    # Create at most one research shadow for a unique bar+gate. Never shadow REFUSED_INSTRUMENT,
    # ACCEPTED, WALLET_BUSY, or non-directional states because those are not useful admission tests.
    excluded = {'REFUSED_INSTRUMENT', 'ACCEPTED', 'WALLET_BUSY', 'UNKNOWN', 'NOT_EXECUTABLE'}
    gate = str(live_gate or 'UNKNOWN')
    key = f'{market}|{setup_key}|{gate}'
    seen = any(r.get('shadow_key') == key for r in rows)
    if direction in {'LONG', 'SHORT'} and gate not in excluded and not seen:
        entry = float(ask if direction == 'LONG' else bid)
        rows.append({
            'shadow_key': key,
            'market': market,
            'gate': gate,
            'regime': str(regime or ('NON_CRYPTO_BASELINE' if market != 'BTC' else 'BTC_REGIME_UNKNOWN')),
            'direction': direction,
            'entry': entry,
            'created_at': now,
            'created_scan': scan_count,
            'status': 'OPEN',
            'age_scans': 0,
            'last_price': entry,
            'last_capital_return_pct': 0.0,
            'last_pnl_usd': 0.0,
            'max_favorable_capital_pct': 0.0,
            'max_adverse_capital_pct': 0.0,
            'reached_15pct_trigger': False,
            'reached_minus10pct_adverse': False,
            'sizing': {
                'paper_lot_size': sizing.get('paper_lot_size'),
                'contract_size': sizing.get('contract_size'),
                'starting_equity_usd': sizing.get('starting_equity_usd'),
                'paper_trade_usd': sizing.get('paper_trade_usd'),
                'leverage': sizing.get('leverage'),
            },
        })

    _save(data_dir, market, rows)
    resolved = [r for r in rows if str(r.get('status', '')).startswith('RESOLVED_')]
    protected = [r for r in resolved if r.get('status') == 'RESOLVED_PROTECTED_LOSER']
    blocked = [r for r in resolved if r.get('status') == 'RESOLVED_BLOCKED_WINNER']
    inconclusive = [r for r in resolved if r.get('status') == 'RESOLVED_INCONCLUSIVE']
    open_rows = [r for r in rows if r.get('status') == 'OPEN']

    by_gate: dict[str, dict] = {}
    gates = sorted({str(r.get('gate')) for r in rows})
    for g in gates:
        rr = [r for r in resolved if str(r.get('gate')) == g]
        p = sum(r.get('status') == 'RESOLVED_PROTECTED_LOSER' for r in rr)
        b = sum(r.get('status') == 'RESOLVED_BLOCKED_WINNER' for r in rr)
        inc = sum(r.get('status') == 'RESOLVED_INCONCLUSIVE' for r in rr)
        decisive = p + b
        by_gate[g] = {
            'resolved': len(rr),
            'protected_losers': p,
            'blocked_winners': b,
            'inconclusive': inc,
            'protect_rate_pct': round(100.0 * p / decisive, 1) if decisive else None,
            'blocked_winner_rate_pct': round(100.0 * b / decisive, 1) if decisive else None,
        }

    return {
        'version': 'COUNTERFACTUAL_GATE_V1',
        'open_shadows': len(open_rows),
        'resolved_shadows': len(resolved),
        'protected_losers': len(protected),
        'blocked_winners': len(blocked),
        'inconclusive': len(inconclusive),
        'trigger_pct': TRIGGER_PCT,
        'adverse_warning_pct': ADVERSE_WARNING_PCT,
        'by_gate': by_gate,
        'recent_resolved': resolved[-10:],
        'guardrail': 'RESEARCH_ONLY_NEVER_SENT_TO_TRADEHOUSE',
    }
