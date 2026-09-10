from __future__ import annotations

"""Evidence-driven learning layer for Companion strategy tournaments.

This module never changes live routing or loosens an execution gate. It records
forward-only scan evidence and produces conservative recommendations that can be
reviewed before any strategy/policy change is made.
"""

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LIVE_PATHS = {
    'USOIL': 'BALANCED_CLEAN',
    'BTC': 'GOLD_M30_LOCAL_STRUCTURE',
}

MIN_RANK_SAMPLE = 30
MIN_PROMOTION_RESOLVED = 200
MIN_FIRST_LOCK_RATE = 80.0
MAX_PROMOTION_ADVERSE_ATR = 2.0
MIN_CHALLENGER_EDGE = 0.75
HISTORY_LIMIT = 2500


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _history_path(data_dir: Path, market: str) -> Path:
    return data_dir / 'learning' / f'{market.lower()}_scan_history.jsonl'


def _append_history(data_dir: Path, market: str, record: dict) -> None:
    path = _history_path(data_dir, market)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, sort_keys=True, default=str) + '\n')

    # Keep disk use bounded while retaining enough history for regime/gate trends.
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
        if len(lines) > HISTORY_LIMIT:
            path.write_text('\n'.join(lines[-HISTORY_LIMIT:]) + '\n', encoding='utf-8')
    except Exception:
        pass


def _read_history(data_dir: Path, market: str, limit: int = 500) -> list[dict]:
    path = _history_path(data_dir, market)
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding='utf-8').splitlines()[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out


def _profile_metrics(row: dict) -> dict:
    opened = _i(row.get('opened'))
    closed = _i(row.get('closed'))
    realized = _f(row.get('realized_pnl_usd'))
    open_pnl = _f(row.get('open_pnl_usd'))
    first_lock = _f(row.get('first_lock_rate_pct'))
    true_conf = _f(row.get('true_confidence_rate_pct'))
    adverse_atr = abs(_f(row.get('worst_adverse_atr')))
    adverse_capital = abs(_f(row.get('worst_adverse_capital_pct')))
    research_score = _f(row.get('research_score'))

    pnl_per_closed = realized / closed if closed else 0.0
    sample_factor = min(1.0, closed / MIN_RANK_SAMPLE) if closed else 0.0
    drawdown_penalty = max(0.0, adverse_atr - 2.0) * 0.6 + max(0.0, adverse_capital - 10.0) * 0.04
    evidence_score = (
        research_score
        + (first_lock / 100.0) * 2.0
        + (true_conf / 100.0) * 2.5
        + max(-2.0, min(2.0, pnl_per_closed / 10.0))
        - drawdown_penalty
    ) * (0.35 + 0.65 * sample_factor)

    return {
        'opened': opened,
        'closed': closed,
        'realized_pnl_usd': round(realized, 2),
        'open_pnl_usd': round(open_pnl, 2),
        'pnl_per_closed_usd': round(pnl_per_closed, 2),
        'first_lock_rate_pct': round(first_lock, 2),
        'true_confidence_rate_pct': round(true_conf, 2),
        'worst_adverse_atr': round(adverse_atr, 3),
        'worst_adverse_capital_pct': round(adverse_capital, 2),
        'research_score': round(research_score, 3),
        'evidence_score': round(evidence_score, 3),
        'sample_maturity_pct': round(sample_factor * 100.0, 1),
    }


def build_learning_report(
    data_dir: Path,
    market: str,
    ranking: list[dict],
    live_gate: str,
    delivery: dict | None,
    context: dict | None,
    scan_count: int,
) -> dict:
    """Persist one scan and return a conservative learning/recommendation report."""
    now = _utcnow()
    live_path = LIVE_PATHS.get(market)
    context = context or {}
    delivery = delivery or {}

    compact_profiles = []
    by_profile: dict[str, dict] = {}
    for row in ranking or []:
        name = str(row.get('profile') or '')
        if not name:
            continue
        metrics = _profile_metrics(row)
        item = {
            'profile': name,
            'family': row.get('family'),
            'last_decision': row.get('last_decision'),
            **metrics,
        }
        compact_profiles.append(item)
        by_profile[name] = item

    scan_record = {
        'at': now,
        'scan_count': scan_count,
        'live_gate': live_gate,
        'delivery_status': delivery.get('status') or delivery.get('lifecycle_state'),
        'profiles': compact_profiles,
        'crypto_governor': (context.get('crypto_breadth') or {}).get('state'),
        'crypto_pressure': (context.get('crypto_breadth') or {}).get('pressure_score'),
    }
    _append_history(data_dir, market, scan_record)
    history = _read_history(data_dir, market)

    gate_counts: Counter[str] = Counter()
    profile_gate_counts: dict[str, Counter[str]] = defaultdict(Counter)
    governor_counts: Counter[str] = Counter()
    for h in history:
        g = str(h.get('live_gate') or 'UNKNOWN')
        gate_counts[g] += 1
        if h.get('crypto_governor'):
            governor_counts[str(h['crypto_governor'])] += 1
        for p in h.get('profiles') or []:
            profile_gate_counts[str(p.get('profile'))][str(p.get('last_decision') or 'UNKNOWN')] += 1

    champion = by_profile.get(live_path) if live_path else None
    sorted_evidence = sorted(compact_profiles, key=lambda x: x['evidence_score'], reverse=True)
    best_research = sorted_evidence[0] if sorted_evidence else None

    promotion_candidates = []
    if champion:
        champion_score = champion['evidence_score']
        for p in sorted_evidence:
            if p['profile'] == live_path:
                continue
            ready = (
                p['closed'] >= MIN_PROMOTION_RESOLVED
                and p['first_lock_rate_pct'] >= MIN_FIRST_LOCK_RATE
                and p['true_confidence_rate_pct'] >= MIN_FIRST_LOCK_RATE
                and p['worst_adverse_atr'] <= MAX_PROMOTION_ADVERSE_ATR
                and p['realized_pnl_usd'] > 0
                and p['evidence_score'] >= champion_score + MIN_CHALLENGER_EDGE
            )
            if ready:
                promotion_candidates.append({
                    'profile': p['profile'],
                    'reason': 'EVIDENCE_THRESHOLD_MET_FOR_HUMAN_REVIEW',
                    'evidence_score': p['evidence_score'],
                    'closed': p['closed'],
                })

    observations: list[str] = []
    actions: list[str] = []
    warnings: list[str] = []

    if champion:
        if champion['closed'] < MIN_RANK_SAMPLE:
            observations.append(
                f"Live champion {live_path} is still low-sample ({champion['closed']}/{MIN_RANK_SAMPLE} resolved for mature ranking)."
            )
        if champion['realized_pnl_usd'] > 0:
            observations.append(
                f"Live champion {live_path} remains profitable in paper evidence: ${champion['realized_pnl_usd']:.2f} realized."
            )
        if champion['true_confidence_rate_pct'] < 60 and champion['opened'] >= 2:
            warnings.append(
                f"{live_path} true-confidence is only {champion['true_confidence_rate_pct']:.1f}%; collect more evidence before increasing trust."
            )
        if champion['worst_adverse_capital_pct'] >= 10:
            warnings.append(
                f"{live_path} has experienced {champion['worst_adverse_capital_pct']:.1f}% adverse capital excursion in paper research."
            )

    if best_research and champion and best_research['profile'] != live_path:
        observations.append(
            f"Current research leader is {best_research['profile']} (evidence score {best_research['evidence_score']}) versus live {live_path} ({champion['evidence_score']})."
        )
        if best_research['closed'] < MIN_PROMOTION_RESOLVED:
            actions.append(
                f"Keep {best_research['profile']} paper-only until at least {MIN_PROMOTION_RESOLVED} resolved trades and promotion-quality drawdown/lock evidence."
            )

    if history:
        top_gates = gate_counts.most_common(3)
        observations.append(
            'Recent live-gate distribution: ' + ', '.join(f'{name} {count}/{len(history)}' for name, count in top_gates) + '.'
        )
        dominant_gate, dominant_count = top_gates[0]
        if dominant_count / len(history) >= 0.70 and dominant_gate not in {'ACCEPTED', 'WALLET_BUSY'}:
            actions.append(
                f"Study {dominant_gate} as the dominant opportunity filter ({dominant_count}/{len(history)} scans); do not loosen it without controlled paper evidence showing better profit and drawdown."
            )

    if market == 'BTC' and governor_counts:
        state, count = governor_counts.most_common(1)[0]
        observations.append(f'BTC market-governor dominant state: {state} ({count}/{sum(governor_counts.values())} recorded scans).')

    if promotion_candidates:
        actions.append('One or more challengers meet the conservative promotion-review threshold; require explicit human review before changing the live path.')
    else:
        actions.append('No challenger currently meets the automatic evidence threshold for replacing the live path.')

    actions.append('Preserve fail-closed live routing and current TradeHouse policy; optimize only through forward paper evidence, never by forcing a live signal.')

    return {
        'version': 'COMPANION_LEARNING_V1',
        'updated_at': now,
        'market': market,
        'scans_recorded': len(history),
        'live_path': live_path,
        'champion': champion,
        'research_leader': best_research,
        'gate_frequency': dict(gate_counts.most_common()),
        'profile_gate_frequency': {k: dict(v.most_common()) for k, v in profile_gate_counts.items()},
        'promotion_candidates': promotion_candidates,
        'observations': observations,
        'recommended_actions': actions,
        'warnings': warnings,
        'guardrails': {
            'automatic_live_strategy_change': False,
            'automatic_gate_loosening': False,
            'minimum_rank_sample': MIN_RANK_SAMPLE,
            'minimum_resolved_for_promotion_review': MIN_PROMOTION_RESOLVED,
            'minimum_first_lock_rate_pct': MIN_FIRST_LOCK_RATE,
            'maximum_worst_adverse_atr': MAX_PROMOTION_ADVERSE_ATR,
            'requires_human_review_for_live_change': True,
        },
    }
