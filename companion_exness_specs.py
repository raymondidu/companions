from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class ExnessSpec:
    market_key: str; broker_symbol: str; contract_size: float; min_lot: float; lot_step: float; pip_size: float; margin_rate: float | None; raw_commission_per_lot_side: float | None; lot_ladder: tuple[float, ...]; gold_0006_equivalent_lot: float | None

SPECS={
    'SILVER':ExnessSpec('SILVER','XAGUSD',5000.0,0.01,0.01,0.01,None,3.50,(0.01,0.02,0.03,0.05),0.01),
    'USOIL':ExnessSpec('USOIL','USOIL',1000.0,0.01,0.01,0.01,0.0005,3.50,(0.01,0.02,0.03,0.05,0.10),0.02),
    'BTC':ExnessSpec('BTC','BTCUSD',1.0,0.01,0.01,0.1,0.0025,None,(0.01,0.02,0.05,0.10),None),
}

CATALOG_VERIFIED_AT='2026-09-08'
CATALOG_SOURCE='EXNESS_OFFICIAL_INSTRUMENT_CATALOG'

def validate_market_mapping(key:str,broker_symbol:str)->ExnessSpec:
    s=spec(key)
    if s.broker_symbol != str(broker_symbol):
        raise ValueError(f'EXNESS_SYMBOL_MISMATCH:{key}:{broker_symbol}:{s.broker_symbol}')
    return s

def spec(key:str)->ExnessSpec:return SPECS[str(key).upper()]
def gross_pnl_usd(key:str,lot:float,entry:float,exit_price:float,direction:str)->float:
    s=spec(key); delta=(exit_price-entry) if direction.upper()=='LONG' else (entry-exit_price); return float(delta)*s.contract_size*float(lot)
def raw_round_trip_commission_usd(key:str,lot:float)->float|None:
    c=spec(key).raw_commission_per_lot_side; return None if c is None else 2.0*c*float(lot)
