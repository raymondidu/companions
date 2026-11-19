from __future__ import annotations

"""Immutable contracts for non-Gold companion research."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CompanionMarket:
    key: str
    display_name: str
    broker_symbol: str
    provider_symbol: str | None
    asset_class: str
    enabled_for_paper: bool
    liquidity_approved: bool
    liquidity_basis: str
    live_authority: bool = False
    tradehouse_delivery: bool = False
    starting_equity_usd: float = 1000.0
    first_trigger_atr: float = 1.5
    first_lock_atr: float = 0.75
    step_atr: float = 0.50
    max_concurrent_positions: int = 1
    policy_state: str = "RESEARCH_SEED_NOT_PROMOTED"


MARKETS: dict[str, CompanionMarket] = {
    "SILVER": CompanionMarket(
        key="SILVER", display_name="Silver Intelligence Lab", broker_symbol="XAGUSD",
        provider_symbol="XAG_USD", asset_class="METAL", enabled_for_paper=True,
        liquidity_approved=True,
        liquidity_basis="CME silver complex has persistent high daily notional and contract volume",
        first_trigger_atr=1.25, first_lock_atr=0.60, step_atr=0.50,
    ),
    "USOIL": CompanionMarket(
        key="USOIL", display_name="Oil Intelligence Lab", broker_symbol="USOIL",
        provider_symbol="WTICO_USD", asset_class="ENERGY", enabled_for_paper=True,
        liquidity_approved=True,
        liquidity_basis="WTI is a global benchmark with consistently deep daily futures activity",
        first_trigger_atr=1.50, first_lock_atr=0.70, step_atr=0.60,
    ),
    "BTC": CompanionMarket(
        key="BTC", display_name="BTC Weekend Lab", broker_symbol="BTCUSD",
        provider_symbol="BTC-USD", asset_class="CRYPTO", enabled_for_paper=True,
        liquidity_approved=True,
        liquidity_basis="Bitcoin has persistent 24/7 spot and derivatives liquidity",
        first_trigger_atr=1.80, first_lock_atr=0.90, step_atr=0.75,
    ),
}


def market(key: str) -> CompanionMarket:
    return MARKETS[str(key).upper()]


def isolation_contract() -> dict:
    return {
        "paper_only": True,
        "live_authority": False,
        "tradehouse_delivery": False,
        "shares_gold_database": False,
        "shares_gold_wallet": False,
        "shares_gold_position_capacity": False,
        "shares_gold_server": False,
        "dedicated_server_required": True,
        "can_modify_gold_signal": False,
        "copies_gold_thresholds": False,
        "liquidity_gate_required": True,
        "promotion_requires_separate_explicit_code_change": True,
        "markets": {k: vars(v) for k, v in MARKETS.items()},
    }
