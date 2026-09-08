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
    family: str = 'BASELINE'
    mode: str = 'BASELINE'
    asset_classes: tuple[str, ...] = ()
    execution_model: str = 'ATR_LOCK_NO_HARD_STOP'
    leverage: float = 1.0
    paper_capital_usd: float = 1000.0


PROFILES: tuple[EntryProfile, ...] = (
    EntryProfile('STRICT_PRECISION', 82, ('TREND_CONTINUATION','BREAKOUT_CONTINUATION'), True, 0.12, (54,72), (28,46)),
    EntryProfile('BALANCED_CLEAN', 74, ('TREND_CONTINUATION','BREAKOUT_CONTINUATION'), True, 0.16, (52,76), (24,48)),
    EntryProfile('TREND_ONLY', 72, ('TREND_CONTINUATION',), False, 0.18, (51,78), (22,49)),
    EntryProfile('BREAKOUT_ONLY', 72, ('BREAKOUT_CONTINUATION',), False, 0.18, (50,82), (18,50)),
    EntryProfile('ELITE_ONLY', 88, ('TREND_CONTINUATION','BREAKOUT_CONTINUATION'), True, 0.10, (55,70), (30,45)),
    EntryProfile('GOLD_HTF_PRECISION',82,('TREND_CONTINUATION',),True,0.12,(52,74),(26,48),'GOLD_TRANSFER','GOLD_HTF'),
    EntryProfile('GOLD_EMA_PULLBACK',80,('EMA_PULLBACK',),False,0.14,(48,70),(30,52),'GOLD_TRANSFER','GOLD_EMA'),
    EntryProfile('GOLD_BREAKOUT_RETEST',80,('BREAKOUT_RETEST',),False,0.14,(48,76),(24,52),'GOLD_TRANSFER','GOLD_RETEST'),
    EntryProfile('GOLD_M30_LOCAL_STRUCTURE',76,('M30_LOCAL_STRUCTURE',),False,0.16,(45,78),(22,55),'GOLD_TRANSFER','M30_STRUCTURE'),
    EntryProfile('GOLD_M30_LIQUIDITY_SWEEP',78,('M30_LIQUIDITY_SWEEP',),False,0.16,(42,75),(25,58),'GOLD_TRANSFER','M30_SWEEP'),
    EntryProfile('CRYPTO_CLEAN_PATH_5X_STOP6',42,('TREND_CONTINUATION','EMA_PULLBACK'),False,0.18,(45,70),(30,55),'CRYPTO_TRANSFER','CRYPTO_CLEAN',('CRYPTO',),'CAPITAL_6_4_5X_STOP6',5.0,100.0),
    EntryProfile('CRYPTO_CLEAN_PATH_5X_NOSTOP',42,('TREND_CONTINUATION','EMA_PULLBACK'),False,0.18,(45,70),(30,55),'CRYPTO_TRANSFER','CRYPTO_CLEAN',('CRYPTO',),'CAPITAL_6_4_5X_NOSTOP',5.0,100.0),
)


def _signal(market,profile,direction,score,setup,atr,bid,ask,reasons):
    ref=float(ask if direction=='LONG' else bid)
    return CompanionSignal(market.key,market.broker_symbol,direction,min(int(score),100),setup,ref,atr,pd.Timestamp.utcnow().isoformat(),tuple([f'{profile.family}:{profile.key}',*reasons]))


def _m30(m15: pd.DataFrame) -> pd.DataFrame:
    d=m15.copy().reset_index(drop=True); rows=[]; start=max(0,len(d)-120)
    if (len(d)-start)%2:start+=1
    for i in range(start,len(d)-1,2):
        a,b=d.iloc[i],d.iloc[i+1]
        rows.append({'time':b['time'],'open':float(a['open']),'high':max(float(a['high']),float(b['high'])),'low':min(float(a['low']),float(b['low'])),'close':float(b['close']),'volume':float(a['volume'])+float(b['volume'])})
    return pd.DataFrame(rows)


def _trend(a15,a1,a4):
    r15,r1,r4=a15.iloc[-1],a1.iloc[-1],a4.iloc[-1]
    long_votes=sum((r15.ema20>r15.ema50,r1.ema20>r1.ema50,r4.ema20>r4.ema50))
    short_votes=sum((r15.ema20<r15.ema50,r1.ema20<r1.ema50,r4.ema20<r4.ema50))
    if long_votes>=2:return 'LONG',long_votes
    if short_votes>=2:return 'SHORT',short_votes
    return None,max(long_votes,short_votes)


def _gold_candidate(market,profile,a15,a1,a4,bid,ask):
    r15,r1,r4=a15.iloc[-1],a1.iloc[-1],a4.iloc[-1]; p15=a15.iloc[-2]
    atr=float(r15.atr14); spread=max(0.0,float(ask)-float(bid)); volume=float(r15.volume/max(float(r15.volume_ma20 or 0),1e-9)); adx=float(r15.adx14 or 0)
    direction,votes=_trend(a15,a1,a4)
    if direction is None:return None,'DIRECTION_ALIGNMENT_BELOW_2_OF_3'
    htf_long=r4.ema20>r4.ema50 and r1.ema20>r1.ema50; htf_short=r4.ema20<r4.ema50 and r1.ema20<r1.ema50
    if (direction=='LONG' and not htf_long) or (direction=='SHORT' and not htf_short):return None,'H1_H4_STRUCTURE_CONFLICT'
    if spread/atr>profile.max_spread_atr:return None,'SPREAD_TO_ATR_TOO_WIDE'
    loc=float(r15.close_location if pd.notna(r15.close_location) else .5); body=float(r15.body_ratio if pd.notna(r15.body_ratio) else 0)
    confirmed=body>=.40 and (loc>=.58 if direction=='LONG' else loc<=.42)
    score=38+8+12*(volume>=1.0)+8*(votes==3); reasons=['H1_H4_STRUCTURE_PERMISSION',f'{votes}_OF_3_TREND',f'ADX_{adx:.1f}',f'VOLUME_{volume:.2f}X']
    setup='NONE'
    if profile.mode=='GOLD_HTF':
        momentum=(r15.close>r15.ema20 and profile.rsi_long[0]<=r15.rsi14<=profile.rsi_long[1]) if direction=='LONG' else (r15.close<r15.ema20 and profile.rsi_short[0]<=r15.rsi14<=profile.rsi_short[1])
        elite=adx>=18 and volume>=1.25 and confirmed and votes==3
        if not momentum:return None,'M15_MOMENTUM_NOT_CONFIRMED'
        if adx<24 and not elite:return None,'ADX_BELOW_24_WITHOUT_ELITE_CONFIRMATION'
        if not confirmed:return None,'ENTRY_CANDLE_NOT_CONFIRMED'
        setup='TREND_CONTINUATION';score+=22+12
    elif profile.mode=='GOLD_EMA':
        touched=(p15.low<=p15.ema20+.15*atr and r15.close>r15.ema20 and r15.close>p15.close) if direction=='LONG' else (p15.high>=p15.ema20-.15*atr and r15.close<r15.ema20 and r15.close<p15.close)
        if not touched:return None,'EMA20_PULLBACK_RESUMPTION_NOT_CONFIRMED'
        if adx<20:return None,'ADX_BELOW_20'
        if volume<.80:return None,'VOLUME_BELOW_0_8X'
        setup='EMA_PULLBACK';score+=24+10
    elif profile.mode=='GOLD_RETEST':
        prior=a15.iloc[-23:-2]
        high=float(prior.high.max());low=float(prior.low.min())
        retest=(p15.close>high and r15.low<=high+.20*atr and r15.close>high and confirmed) if direction=='LONG' else (p15.close<low and r15.high>=low-.20*atr and r15.close<low and confirmed)
        if not retest:return None,'BREAKOUT_RETEST_NOT_CONFIRMED'
        if adx<22:return None,'ADX_BELOW_22'
        if volume<.90:return None,'VOLUME_BELOW_0_9X'
        setup='BREAKOUT_RETEST';score+=26+10
    return (_signal(market,profile,direction,score,setup,atr,bid,ask,reasons), 'ACCEPTED') if setup!='NONE' and score>=profile.min_score else (None,'PROFILE_SCORE_BELOW_FLOOR')


def _m30_candidate(market,profile,m15,h1,h4,bid,ask):
    m30=_m30(m15)
    if len(m30)<12:return None,'INSUFFICIENT_M30_HISTORY'
    a15,a1,a4=enrich(m15),enrich(h1),enrich(h4);direction,_=_trend(a15,a1,a4)
    if direction is None:return None,'DIRECTION_ALIGNMENT_BELOW_2_OF_3'
    r1,r4=a1.iloc[-1],a4.iloc[-1]
    if direction=='LONG' and not (r1.ema20>r1.ema50 and r4.ema20>r4.ema50):return None,'H1_H4_STRUCTURE_CONFLICT'
    if direction=='SHORT' and not (r1.ema20<r1.ema50 and r4.ema20<r4.ema50):return None,'H1_H4_STRUCTURE_CONFLICT'
    e30=enrich(m30);c=e30.iloc[-1];p=e30.iloc[-2];atr=float(c.atr14);spread=max(0,float(ask)-float(bid))
    if not atr or spread/atr>profile.max_spread_atr:return None,'SPREAD_TO_ATR_TOO_WIDE'
    recent=e30.iloc[-9:-1]; hi=float(recent.high.max());lo=float(recent.low.min());rng=max(float(c.high-c.low),1e-9)
    upper=(float(c.high)-max(float(c.open),float(c.close)))/rng;lower=(min(float(c.open),float(c.close))-float(c.low))/rng
    if profile.mode=='M30_SWEEP':
        ok=(direction=='SHORT' and c.high>hi and c.close<hi and upper>=.20) or (direction=='LONG' and c.low<lo and c.close>lo and lower>=.20)
        if not ok:return None,'M30_LIQUIDITY_SWEEP_NOT_CONFIRMED'
        return _signal(market,profile,direction,82,'M30_LIQUIDITY_SWEEP',atr,bid,ask,['H1_H4_PERMISSION','M30_SWEEP_AND_REJECTION']), 'ACCEPTED'
    older=e30.iloc[-6:-3];newer=e30.iloc[-3:]
    ok=(direction=='SHORT' and newer.high.max()<older.high.max() and newer.low.min()<older.low.min() and c.close<p.low) or (direction=='LONG' and newer.high.max()>older.high.max() and newer.low.min()>older.low.min() and c.close>p.high)
    if not ok:return None,'M30_LOCAL_STRUCTURE_BREAK_NOT_CONFIRMED'
    return _signal(market,profile,direction,80,'M30_LOCAL_STRUCTURE',atr,bid,ask,['H1_H4_PERMISSION','M30_STRUCTURE_BREAK']), 'ACCEPTED'


def _crypto_candidate(market,profile,a15,a1,a4,bid,ask,context):
    r15,r1,r4=a15.iloc[-1],a1.iloc[-1],a4.iloc[-1];atr=float(r15.atr14);direction,votes=_trend(a15,a1,a4)
    if direction is None:return None,'DIRECTION_ALIGNMENT_BELOW_2_OF_3'
    breadth=context.get('crypto_breadth') or {}
    if not breadth:return None,'CRYPTO_MARKET_BREADTH_UNAVAILABLE'
    if direction=='LONG' and not breadth.get('long_allowed'):return None,'MARKET_DIRECTION_GOVERNOR_PAUSED_LONG'
    if direction=='SHORT' and not breadth.get('short_allowed'):return None,'MARKET_DIRECTION_GOVERNOR_PAUSED_SHORT'
    volume=float(r15.volume/max(float(r15.volume_ma20 or 0),1e-9));extension=abs(100*(float(r15.close)/float(r15.ema20)-1));day=abs(100*(float(r15.close)/float(a15.iloc[-97].close)-1)) if len(a15)>=97 else 0
    sign=1 if direction=='LONG' else -1;m15=sign*float(r15.momentum_3 or 0);h1=sign*float(r1.momentum_3 or 0);h4=sign*float(r4.momentum_3 or 0);rsi=float(r15.rsi14 or 50);spread_atr=max(0,float(ask)-float(bid))/atr
    if spread_atr>1.0:return None,'EXECUTION_SPREAD_CATASTROPHIC'
    if extension>6:return None,'EXTENSION_CATASTROPHIC'
    if m15<-2 and h1<-2:return None,'MOMENTUM_STRONGLY_OPPOSES_DIRECTION'
    score=0
    score+=26 if extension<=1.5 else 10 if extension<=3 else 0
    score+=12 if votes==3 else 7
    score+=16 if .25<=m15<=.50 else 10 if 0<=m15<.25 else 6 if .50<m15<=2 else 0
    score+=12 if 0<=h1<.5 else 6 if .5<=h1<=2 else 0
    score+=18 if 20<=day<=30 else 10 if day<5 else 6 if day<=20 else 0
    score+=14 if ((direction=='LONG' and 50<=rsi<=60) or (direction=='SHORT' and 40<=rsi<=50)) else 7
    score+=8 if .70<=volume<=1.25 else 5 if 1.25<volume<=2.5 else 0
    score+=3 if h4>=0 else 0
    if score<profile.min_score:return None,f'CLEAN_PATH_COMPOSITE_BELOW_{profile.min_score}'
    setup='EMA_PULLBACK' if extension<=.35 else 'TREND_CONTINUATION'
    reasons=[f'BREADTH_{breadth.get("state")}',f'PRESSURE_{breadth.get("pressure_score")}',f'{votes}_OF_3_ALIGNMENT',f'CLEAN_PATH_SCORE_{score}']
    return _signal(market,profile,direction,score,setup,atr,bid,ask,reasons),'ACCEPTED'


def evaluate_profile(market: CompanionMarket, profile: EntryProfile, m15: pd.DataFrame, h1: pd.DataFrame, h4: pd.DataFrame, bid: float, ask: float, context: dict | None = None):
    if min(len(m15),len(h1),len(h4)) < 60:
        return None,'INSUFFICIENT_HISTORY'
    if profile.asset_classes and market.asset_class not in profile.asset_classes:
        return None,'NOT_APPLICABLE_TO_ASSET_CLASS'
    context=context or {}
    a15,a1,a4=enrich(m15),enrich(h1),enrich(h4)
    if profile.mode.startswith('GOLD_'):
        return _gold_candidate(market,profile,a15,a1,a4,bid,ask)
    if profile.mode in {'M30_STRUCTURE','M30_SWEEP'}:
        return _m30_candidate(market,profile,m15,h1,h4,bid,ask)
    if profile.mode=='CRYPTO_CLEAN':
        return _crypto_candidate(market,profile,a15,a1,a4,bid,ask,context)
    r15,r1,r4=a15.iloc[-1],a1.iloc[-1],a4.iloc[-1]
    if pd.isna(r15.atr14) or float(r15.atr14) <= 0:
        return None,'ATR_UNAVAILABLE'
    atr=float(r15.atr14); spread=max(0.0,float(ask)-float(bid))
    if spread/atr > profile.max_spread_atr:
        return None,'SPREAD_TO_ATR_TOO_WIDE'
    long_trend=r4.ema20>r4.ema50 and r1.ema20>r1.ema50
    short_trend=r4.ema20<r4.ema50 and r1.ema20<r1.ema50
    if not (long_trend or short_trend):
        return None,'H1_H4_TREND_NOT_ALIGNED'
    volume_ok=bool(pd.notna(r15.volume_ma20) and r15.volume >= 0.85*r15.volume_ma20)
    if profile.require_volume and not volume_ok:
        return None,'VOLUME_BELOW_0_85X'
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
        return None,'SETUP_OR_SCORE_BELOW_PROFILE_FLOOR'
    ref=float(ask if direction=='LONG' else bid)
    return CompanionSignal(market.key,market.broker_symbol,direction,min(score,100),setup,ref,atr,pd.Timestamp.utcnow().isoformat(),tuple(reasons)),'ACCEPTED'


def candidate_for_profile(market: CompanionMarket, profile: EntryProfile, m15: pd.DataFrame, h1: pd.DataFrame, h4: pd.DataFrame, bid: float, ask: float, context: dict | None = None) -> CompanionSignal | None:
    return evaluate_profile(market,profile,m15,h1,h4,bid,ask,context)[0]


class Tournament:
    def __init__(self, base_dir: str | Path, market: CompanionMarket):
        self.base_dir=Path(base_dir); self.market=market
        self.base_dir.mkdir(parents=True,exist_ok=True)

    def store(self, profile: EntryProfile) -> CompanionStore:
        policy={'family':profile.family,'execution_model':profile.execution_model,'leverage':profile.leverage,'paper_capital_usd':profile.paper_capital_usd}
        return CompanionStore(self.base_dir/f'{self.market.key.lower()}__{profile.key.lower()}.db',self.market,policy)

    def step(self, m15: pd.DataFrame, h1: pd.DataFrame, h4: pd.DataFrame, bid: float, ask: float, context: dict | None = None) -> dict:
        out={}
        for profile in PROFILES:
            store=self.store(profile)
            # Manage from the fresh quote only. Reusing the last completed M15
            # high/low after a new entry would leak pre-entry price movement.
            store.mark(bid,ask)
            sig=None
            decision='WALLET_BUSY'
            if not store.has_open():
                sig,decision=evaluate_profile(self.market,profile,m15,h1,h4,bid,ask,context)
                if sig is not None: store.record(sig)
            s=store.summary(); s['profile']=profile.key; s['family']=profile.family; s['min_score']=profile.min_score
            s['candidate']=vars(sig) if sig else None
            s['last_decision']=decision
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
            policy=s.get('research_policy') or {}
            rows.append({'profile':key,'family':s.get('family'),'execution_model':policy.get('execution_model'),'paper_capital_usd':policy.get('paper_capital_usd'),'leverage':policy.get('leverage'),'last_decision':s.get('last_decision'),'opened':opened,'closed':closed,'first_locks':locks,'first_lock_rate_pct':round(lock_rate,2),'true_confidence_rate_pct':s.get('true_confidence_rate_pct',0),'realized_price_return_sum_pct':round(realized,4),'worst_adverse_atr':round(worst,3),'worst_adverse_capital_pct':s.get('worst_adverse_capital_pct',0),'evidence_weight':round(evidence,3),'research_score':round(quality*evidence,3),'promotion_ready': closed>=200 and lock_rate>=80 and worst<=2.0})
        return sorted(rows,key=lambda x:(x['promotion_ready'],x['research_score'],x['first_lock_rate_pct']),reverse=True)
