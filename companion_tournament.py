from __future__ import annotations

"""Forward-only strategy tournament for isolated companion markets."""
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from companion_engine import CompanionSignal, CompanionStore, enrich
from companion_markets import CompanionMarket


@dataclass(frozen=True)
class EntryProfile:
    key: str
    min_score: int
    setups: tuple[str, ...]
    require_volume: bool
    max_spread_atr: float
    rsi_long: tuple[float, float]
    rsi_short: tuple[float, float]


PROFILES: tuple[EntryProfile, ...] = (
    EntryProfile('STRICT_PRECISION', 82, ('TREND_CONTINUATION','BREAKOUT_CONTINUATION'), True, 0.12, (54,72), (28,46)),
    EntryProfile('BALANCED_CLEAN', 74, ('TREND_CONTINUATION','BREAKOUT_CONTINUATION'), True, 0.16, (52,76), (24,48)),
    EntryProfile('TREND_ONLY', 72, ('TREND_CONTINUATION',), False, 0.18, (51,78), (22,49)),
    EntryProfile('BREAKOUT_ONLY', 72, ('BREAKOUT_CONTINUATION',), False, 0.18, (50,82), (18,50)),
    EntryProfile('ELITE_ONLY', 88, ('TREND_CONTINUATION','BREAKOUT_CONTINUATION'), True, 0.10, (55,70), (30,45)),
)


def candidate_for_profile(market: CompanionMarket, profile: EntryProfile, m15: pd.DataFrame, h1: pd.DataFrame, h4: pd.DataFrame, bid: float, ask: float) -> CompanionSignal | None:
    if min(len(m15),len(h1),len(h4)) < 60:
        return None
    a15,a1,a4=enrich(m15),enrich(h1),enrich(h4)
    r15,r1,r4=a15.iloc[-1],a1.iloc[-1],a4.iloc[-1]
    if pd.isna(r15.atr14) or float(r15.atr14) <= 0:
        return None
    atr=float(r15.atr14); spread=max(0.0,float(ask)-float(bid))
    if spread/atr > profile.max_spread_atr:
        return None
    long_trend=r4.ema20>r4.ema50 and r1.ema20>r1.ema50
    short_trend=r4.ema20<r4.ema50 and r1.ema20<r1.ema50
    if not (long_trend or short_trend):
        return None
    volume_ok=bool(pd.notna(r15.volume_ma20) and r15.volume >= 0.85*r15.volume_ma20)
    if profile.require_volume and not volume_ok:
        return None
    long_momo=r15.close>r15.ema20 and profile.rsi_long[0] <= r15.rsi14 <= profile.rsi_long[1]
    short_momo=r15.close<r15.ema20 and profile.rsi_short[0] <= r15.rsi14 <= profile.rsi_short[1]
    long_break=pd.notna(r15.range_high20) and r15.close>r15.range_high20
    short_break=pd.notna(r15.range_low20) and r15.close<r15.range_low20
    direction='LONG' if long_trend else 'SHORT'
    setup='NONE'; score=38; reasons=[f'{profile.key}_PROFILE','H1_H4_TREND']
    if direction=='LONG':
        if long_momo: score+=22; setup='TREND_CONTINUATION'; reasons.append('M15_MOMENTUM')
        if long_break: score+=24; setup='BREAKOUT_CONTINUATION'; reasons.append('M15_BREAKOUT')
    else:
        if short_momo: score+=22; setup='TREND_CONTINUATION'; reasons.append('M15_MOMENTUM')
        if short_break: score+=24; setup='BREAKOUT_CONTINUATION'; reasons.append('M15_BREAKOUT')
    if volume_ok: score+=12; reasons.append('VOLUME_OK')
    score+=8; reasons.append('SPREAD_ATR_OK')
    if setup not in profile.setups or score < profile.min_score:
        return None
    ref=float(ask if direction=='LONG' else bid)
    return CompanionSignal(market.key,market.broker_symbol,direction,min(score,100),setup,ref,atr,pd.Timestamp.utcnow().isoformat(),tuple(reasons))


class Tournament:
    def __init__(self, base_dir: str | Path, market: CompanionMarket):
        self.base_dir=Path(base_dir); self.market=market
        self.base_dir.mkdir(parents=True,exist_ok=True)

    def store(self, profile: EntryProfile) -> CompanionStore:
        return CompanionStore(self.base_dir/f'{self.market.key.lower()}__{profile.key.lower()}.db',self.market)

    def step(self, m15: pd.DataFrame, h1: pd.DataFrame, h4: pd.DataFrame, bid: float, ask: float) -> dict:
        high=float(m15.iloc[-1]['high']); low=float(m15.iloc[-1]['low'])
        out={}
        for profile in PROFILES:
            store=self.store(profile)
            store.mark(bid,ask,high,low)
            sig=None
            if not store.has_open():
                sig=candidate_for_profile(self.market,profile,m15,h1,h4,bid,ask)
                if sig is not None: store.record(sig)
            s=store.summary(); s['profile']=profile.key; s['min_score']=profile.min_score
            s['candidate']=vars(sig) if sig else None
            out[profile.key]=s
        return out

    @staticmethod
    def rank(results: dict) -> list[dict]:
        rows=[]
        for key,s in results.items():
            opened=int(s.get('opened',0)); locks=int(s.get('first_locks',0)); closed=int(s.get('closed',0))
            recent=s.get('recent',[]) or []
            realized=sum(float(r.get('price_return_pct') or 0) for r in recent if r.get('status')=='CLOSED')
            worst=float(s.get('worst_adverse_atr') or 0)
            lock_rate=(100*locks/opened) if opened else 0.0
            evidence=min(opened,30)/30.0
            quality=(lock_rate*0.55)+(max(-50.0,min(50.0,realized))*0.25)-(worst*8.0)+(closed*0.20)
            rows.append({'profile':key,'opened':opened,'closed':closed,'first_locks':locks,'first_lock_rate_pct':round(lock_rate,2),'realized_price_return_sum_pct':round(realized,4),'worst_adverse_atr':round(worst,3),'evidence_weight':round(evidence,3),'research_score':round(quality*evidence,3),'promotion_ready': opened>=30 and lock_rate>=80 and worst<=2.0})
        return sorted(rows,key=lambda x:(x['promotion_ready'],x['research_score'],x['first_lock_rate_pct']),reverse=True)
