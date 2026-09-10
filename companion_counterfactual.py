from __future__ import annotations

"""Counterfactual research for rejected Companion live-path setups.

Research-only by construction: this module has no TradeHouse dependency and cannot
send, promote, or alter a live signal. It asks whether a rejected directional setup
was protective, suppressed a profitable opportunity, or still contained useful
partial edge even when neither canonical threshold was reached.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TRIGGER_PCT = 15.0
ADVERSE_WARNING_PCT = -10.0
PARTIAL_FAVORABLE_LEVELS = (5.0, 7.5, 10.0)
HORIZON_SCANS = {'1h': 60, '4h': 240, '12h': 720}
MAX_HOLD_SCANS = 720  # ~12h at 60s scans so inconclusive cases still teach us.
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


def _record_horizons(row: dict, cap: float, pnl: float, now: str) -> None:
    age = int(row.get('age_scans') or 0)
    horizons = row.setdefault('horizon_outcomes', {})
    for label, target in HORIZON_SCANS.items():
        if age >= target and label not in horizons:
            horizons[label] = {
                'capital_return_pct': round(cap, 4),
                'pnl_usd': round(pnl, 4),
                'recorded_at': now,
            }


def _mark_partial_levels(row: dict, now: str) -> None:
    mfe = _f(row.get('max_favorable_capital_pct'))
    reached = row.setdefault('partial_favorable_levels', {})
    for level in PARTIAL_FAVORABLE_LEVELS:
        key = str(level).rstrip('0').rstrip('.')
        if mfe >= level and key not in reached:
            reached[key] = now


def _avg(rows: list[dict], field: str) -> float | None:
    if not rows:
        return None
    return round(sum(_f(r.get(field)) for r in rows) / len(rows), 3)


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

    # Mark all existing open shadows using executable-side prices.
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
        _mark_partial_levels(r, now)
        _record_horizons(r, cap, pnl, now)

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
            r['resolution'] = '12H_HORIZON_EXPIRED_WITHOUT_15PCT_OR_MINUS10PCT'
            r['resolved_at'] = now

    # Policy/portfolio gates are excluded because they are not admission-quality experiments.
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
            'partial_favorable_levels': {},
            'horizon_outcomes': {},
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
        all_gate = [r for r in rows if str(r.get('gate')) == g]
        rr = [r for r in resolved if str(r.get('gate')) == g]
        p = sum(r.get('status') == 'RESOLVED_PROTECTED_LOSER' for r in rr)
        b = sum(r.get('status') == 'RESOLVED_BLOCKED_WINNER' for r in rr)
        inc = sum(r.get('status') == 'RESOLVED_INCONCLUSIVE' for r in rr)
        decisive = p + b
        partial_counts = {}
        for level in PARTIAL_FAVORABLE_LEVELS:
            key_level = str(level).rstrip('0').rstrip('.')
            partial_counts[f'reached_{key_level}pct'] = sum(
                key_level in (r.get('partial_favorable_levels') or {}) for r in all_gate
            )
        horizon_summary = {}
        for label in HORIZON_SCANS:
            samples = [r['horizon_outcomes'][label] for r in all_gate if label in (r.get('horizon_outcomes') or {})]
            horizon_summary[label] = {
                'samples': len(samples),
                'avg_capital_return_pct': round(sum(_f(x.get('capital_return_pct')) for x in samples) / len(samples), 3) if samples else None,
                'positive_rate_pct': round(100.0 * sum(_f(x.get('capital_return_pct')) > 0 for x in samples) / len(samples), 1) if samples else None,
            }
        by_gate[g] = {
            'tracked': len(all_gate),
            'resolved': len(rr),
            'protected_losers': p,
            'blocked_winners': b,
            'inconclusive': inc,
            'protect_rate_pct': round(100.0 * p / decisive, 1) if decisive else None,
            'blocked_winner_rate_pct': round(100.0 * b / decisive, 1) if decisive else None,
            'avg_mfe_capital_pct': _avg(all_gate, 'max_favorable_capital_pct'),
            'avg_mae_capital_pct': _avg(all_gate, 'max_adverse_capital_pct'),
            **partial_counts,
            'horizons': horizon_summary,
        }

    return {
        'version': 'COUNTERFACTUAL_GATE_V2',
        'open_shadows': len(open_rows),
        'resolved_shadows': len(resolved),
        'protected_losers': len(protected),
        'blocked_winners': len(blocked),
        'inconclusive': len(inconclusive),
        'trigger_pct': TRIGGER_PCT,
        'adverse_warning_pct': ADVERSE_WARNING_PCT,
        'partial_favorable_levels_pct': list(PARTIAL_FAVORABLE_LEVELS),
        'horizon_scans': HORIZON_SCANS,
        'max_hold_scans': MAX_HOLD_SCANS,
        'by_gate': by_gate,
        'recent_resolved': resolved[-10:],
        'guardrail': 'RESEARCH_ONLY_NEVER_SENT_TO_TRADEHOUSE',
    }
