from __future__ import annotations

"""Regime-conditioned research for Companion strategy tournaments.

This module is deliberately read-only with respect to live routing. It reconstructs
forward performance by market regime from cumulative tournament snapshots using
scan-to-scan deltas, so old P&L is not counted repeatedly on every scan.
"""

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


LIVE_PATHS = {
    'USOIL': 'BALANCED_CLEAN',
    'BTC': 'GOLD_M30_LOCAL_STRUCTURE',
}
MIN_REGIME_SAMPLE = 30
MIN_REGIME_REVIEW_SAMPLE = 100


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _history(data_dir: Path, market: str, limit: int = 1200) -> list[dict]:
    p = data_dir / 'learning' / f'{market.lower()}_scan_history.jsonl'
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding='utf-8').splitlines()[-limit:]:
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    out.append(row)
            except Exception:
                continue
    except Exception:
        return []
    return out


def _counterfactual_rows(data_dir: Path, market: str) -> list[dict]:
    p = data_dir / 'learning' / f'{market.lower()}_counterfactual.json'
    if not p.exists():
        return []
    try:
        rows = json.loads(p.read_text(encoding='utf-8'))
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _regime_name(market: str, scan: dict) -> str:
    state = str(scan.get('crypto_governor') or '').strip()
    if market == 'BTC':
        return state or 'BTC_REGIME_UNKNOWN'
    return 'NON_CRYPTO_BASELINE'


def build_regime_report(data_dir: Path, market: str) -> dict:
    history = _history(data_dir, market)
    previous: dict[str, dict] = {}
    stats: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: {
        'resolved': 0.0,
        'opened': 0.0,
        'realized_pnl_usd': 0.0,
        'first_locks': 0.0,
        'true_confidence_wins': 0.0,
        'worst_adverse_atr': 0.0,
        'worst_adverse_capital_pct': 0.0,
    }))
    gate_by_regime: dict[str, Counter[str]] = defaultdict(Counter)
    scan_counts: Counter[str] = Counter()

    for scan in history:
        regime = _regime_name(market, scan)
        scan_counts[regime] += 1
        gate_by_regime[regime][str(scan.get('live_gate') or 'UNKNOWN')] += 1
        for p in scan.get('profiles') or []:
            profile = str(p.get('profile') or '')
            if not profile:
                continue
            cur_closed = _i(p.get('closed'))
            cur_opened = _i(p.get('opened'))
            cur_realized = _f(p.get('realized_pnl_usd'))
            cur_first = cur_opened * _f(p.get('first_lock_rate_pct')) / 100.0
            cur_true = cur_opened * _f(p.get('true_confidence_rate_pct')) / 100.0
            prev = previous.get(profile)
            if prev is None:
                previous[profile] = {
                    'closed': cur_closed,
                    'opened': cur_opened,
                    'realized': cur_realized,
                    'first': cur_first,
                    'true': cur_true,
                }
                # The first retained snapshot contains pre-history totals. Do not
                # assign those old outcomes to the first observed regime.
                continue

            d_closed = max(0, cur_closed - _i(prev.get('closed')))
            d_opened = max(0, cur_opened - _i(prev.get('opened')))
            d_realized = cur_realized - _f(prev.get('realized')) if d_closed > 0 else 0.0
            d_first = max(0.0, cur_first - _f(prev.get('first')))
            d_true = max(0.0, cur_true - _f(prev.get('true')))
            s = stats[regime][profile]
            s['resolved'] += d_closed
            s['opened'] += d_opened
            s['realized_pnl_usd'] += d_realized
            s['first_locks'] += d_first
            s['true_confidence_wins'] += d_true
            s['worst_adverse_atr'] = max(s['worst_adverse_atr'], abs(_f(p.get('worst_adverse_atr'))))
            s['worst_adverse_capital_pct'] = max(s['worst_adverse_capital_pct'], abs(_f(p.get('worst_adverse_capital_pct'))))
            previous[profile] = {
                'closed': cur_closed,
                'opened': cur_opened,
                'realized': cur_realized,
                'first': cur_first,
                'true': cur_true,
            }

    rankings: dict[str, list[dict]] = {}
    for regime, profiles in stats.items():
        rows: list[dict] = []
        for profile, s in profiles.items():
            resolved = int(round(s['resolved']))
            opened = int(round(s['opened']))
            pnl = s['realized_pnl_usd']
            first_rate = 100.0 * s['first_locks'] / opened if opened else 0.0
            true_rate = 100.0 * s['true_confidence_wins'] / opened if opened else 0.0
            pnl_per = pnl / resolved if resolved else 0.0
            maturity = min(1.0, resolved / MIN_REGIME_SAMPLE) if resolved else 0.0
            score = (
                max(-3.0, min(3.0, pnl_per / 10.0))
                + 2.0 * first_rate / 100.0
                + 2.5 * true_rate / 100.0
                - max(0.0, s['worst_adverse_capital_pct'] - 10.0) * 0.04
            ) * (0.25 + 0.75 * maturity)
            rows.append({
                'profile': profile,
                'resolved': resolved,
                'opened': opened,
                'realized_pnl_usd': round(pnl, 2),
                'pnl_per_resolved_usd': round(pnl_per, 2),
                'first_lock_rate_pct': round(first_rate, 1),
                'true_confidence_rate_pct': round(true_rate, 1),
                'worst_adverse_atr_seen': round(s['worst_adverse_atr'], 3),
                'worst_adverse_capital_pct_seen': round(s['worst_adverse_capital_pct'], 2),
                'regime_evidence_score': round(score, 3),
                'mature': resolved >= MIN_REGIME_SAMPLE,
            })
        rankings[regime] = sorted(rows, key=lambda x: (x['regime_evidence_score'], x['resolved']), reverse=True)

    cf_by_regime_gate: dict[str, dict[str, dict[str, int | float | None]]] = defaultdict(dict)
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in _counterfactual_rows(data_dir, market):
        if not str(row.get('status') or '').startswith('RESOLVED_'):
            continue
        regime = str(row.get('regime') or 'UNCLASSIFIED')
        gate = str(row.get('gate') or 'UNKNOWN')
        grouped[(regime, gate)].append(row)
    for (regime, gate), rows in grouped.items():
        protected = sum(r.get('status') == 'RESOLVED_PROTECTED_LOSER' for r in rows)
        blocked = sum(r.get('status') == 'RESOLVED_BLOCKED_WINNER' for r in rows)
        inconclusive = sum(r.get('status') == 'RESOLVED_INCONCLUSIVE' for r in rows)
        decisive = protected + blocked
        cf_by_regime_gate[regime][gate] = {
            'resolved': len(rows),
            'protected_losers': protected,
            'blocked_winners': blocked,
            'inconclusive': inconclusive,
            'protect_rate_pct': round(100.0 * protected / decisive, 1) if decisive else None,
            'blocked_winner_rate_pct': round(100.0 * blocked / decisive, 1) if decisive else None,
        }

    current_regime = _regime_name(market, history[-1]) if history else None
    current_ranking = rankings.get(current_regime or '', [])
    live_path = LIVE_PATHS.get(market)
    leader = current_ranking[0] if current_ranking else None
    live_row = next((r for r in current_ranking if r.get('profile') == live_path), None)
    observations: list[str] = []
    recommendations: list[str] = []

    if current_regime:
        observations.append(f'Current regime: {current_regime} ({scan_counts[current_regime]} retained scans).')
    if leader:
        observations.append(
            f"Regime research leader is {leader['profile']} with {leader['resolved']} resolved outcome(s), ${leader['realized_pnl_usd']:.2f} realized in newly attributed regime outcomes, and evidence score {leader['regime_evidence_score']}."
        )
    if leader and live_path and leader['profile'] != live_path:
        if leader['resolved'] < MIN_REGIME_REVIEW_SAMPLE:
            recommendations.append(
                f"Keep {leader['profile']} paper-only in {current_regime}; only {leader['resolved']}/{MIN_REGIME_REVIEW_SAMPLE} regime-resolved outcomes exist for a selector review."
            )
        else:
            recommendations.append(
                f"{leader['profile']} has enough {current_regime} sample for a human regime-selector review; do not switch live routing automatically."
            )
    if live_row and live_row['resolved'] < MIN_REGIME_SAMPLE:
        recommendations.append(
            f"Current live path {live_path} has only {live_row['resolved']}/{MIN_REGIME_SAMPLE} newly attributed resolved outcomes in {current_regime}; keep collecting forward evidence."
        )
    recommendations.append('Regime evidence is research-only and cannot override the live allowlist, live gate, or TradeHouse execution policy.')

    return {
        'version': 'COMPANION_REGIME_LEARNING_V1',
        'market': market,
        'current_regime': current_regime,
        'scan_counts_by_regime': dict(scan_counts),
        'gate_frequency_by_regime': {r: dict(c.most_common()) for r, c in gate_by_regime.items()},
        'rankings_by_regime': rankings,
        'current_regime_ranking': current_ranking,
        'current_regime_leader': leader,
        'current_live_path_regime_stats': live_row,
        'counterfactual_gate_effectiveness_by_regime': dict(cf_by_regime_gate),
        'observations': observations,
        'recommended_actions': recommendations,
        'guardrails': {
            'research_only': True,
            'automatic_live_selector_change': False,
            'minimum_regime_sample': MIN_REGIME_SAMPLE,
            'minimum_regime_review_sample': MIN_REGIME_REVIEW_SAMPLE,
        },
    }
