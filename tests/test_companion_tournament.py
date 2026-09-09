import pandas as pd
import asyncio
import math
from companion_markets import MARKETS
from companion_tournament import COHORT_KEY, PROFILES, Tournament, candidate_for_profile, evaluate_profile
from market_data import BinanceBreadthData, Quote


def frame(n=100,start=100.0,step=0.3):
    rows=[]
    for i in range(n):
        c=start+i*step
        rows.append({'time':pd.Timestamp('2026-01-01',tz='UTC')+pd.Timedelta(minutes=15*i),'open':c-.05,'high':c+.30,'low':c-.15,'close':c,'volume':100+i})
    return pd.DataFrame(rows)


def v2_frame(step, amplitude=.2, phase=.2, n=140):
    rows=[]
    for i in range(n):
        close=100+i*step+math.sin(i*.9+phase)*amplitude
        rows.append({
            'time':pd.Timestamp('2026-01-01',tz='UTC')+pd.Timedelta(minutes=15*i),
            'open':close-.12,'high':close+.04,'low':close-.16,'close':close,
            'volume':100+(i%7)*5,
        })
    return pd.DataFrame(rows)


def strong_long_breadth(pressure=30):
    return {
        'state':'LONG_FAVORED','long_allowed':True,'short_allowed':False,
        'pressure_score':pressure,'volume_up_pct':65,'breadth_up_pct':60,
        'major_up':4,
    }


def test_tournament_has_multiple_independent_profiles():
    assert len(PROFILES) >= 12
    assert len({p.key for p in PROFILES}) == len(PROFILES)
    assert {'BASELINE','GOLD_TRANSFER','CRYPTO_TRANSFER'} <= {p.family for p in PROFILES}
    assert all(p.paper_trade_usd == 200 for p in PROFILES)


def test_each_profile_gets_its_own_wallet_db(tmp_path):
    t=Tournament(tmp_path,MARKETS['SILVER'])
    stores=[t.store(p) for p in PROFILES]
    assert len({s.path for s in stores}) == len(PROFILES)
    assert all('gold.db' not in s.path.lower() for s in stores)
    assert all(COHORT_KEY.lower() in s.path.lower() for s in stores)


def test_market_position_sizes_follow_execution_policy(tmp_path):
    silver=Tournament(tmp_path/'silver',MARKETS['SILVER']).store(PROFILES[0]).summary()['research_policy']
    oil=Tournament(tmp_path/'oil',MARKETS['USOIL']).store(PROFILES[0]).summary()['research_policy']
    btc=Tournament(tmp_path/'btc',MARKETS['BTC']).store(PROFILES[-1]).summary()['research_policy']
    btc_baseline=Tournament(tmp_path/'btc-baseline',MARKETS['BTC']).store(PROFILES[0]).summary()['research_policy']
    assert silver['paper_lot_size'] == 0.01 and silver['paper_trade_usd'] is None
    assert oil['paper_lot_size'] == 0.02 and oil['paper_trade_usd'] is None
    assert btc['paper_lot_size'] is None and btc['paper_trade_usd'] == 200
    assert btc['leverage'] == btc_baseline['leverage'] == 10


def test_profile_candidate_is_forward_paper_only_logic():
    cfg=MARKETS['SILVER']; m15=frame(); h1=frame(step=.5); h4=frame(step=1.0)
    prior=float(m15.iloc[-21:-1]['high'].max()); i=m15.index[-1]
    m15.loc[i,'open']=prior+.2; m15.loc[i,'low']=prior+.1; m15.loc[i,'close']=prior+.8; m15.loc[i,'high']=prior+1.0
    bid=float(m15.loc[i,'close'])-.01; ask=float(m15.loc[i,'close'])+.01
    found=[candidate_for_profile(cfg,p,m15,h1,h4,bid,ask) for p in PROFILES]
    assert any(x is not None for x in found)


def test_ranking_requires_real_sample_before_promotion():
    fake={p.key:{'opened':5,'closed':5,'first_locks':5,'first_lock_rate_pct':100,'worst_adverse_atr':0.5,'recent':[]} for p in PROFILES}
    ranked=Tournament.rank(fake)
    assert ranked
    assert all(x['promotion_ready'] is False for x in ranked)
    assert all(x['rank_eligible'] is False for x in ranked)


def test_ranking_requires_30_resolved_trades_not_open_positions():
    fake={
        p.key:{'opened':30,'closed':29,'first_locks':20,'worst_adverse_atr':1.0,'recent':[]}
        for p in PROFILES
    }
    ranked=Tournament.rank(fake)
    assert ranked and all(x['rank_eligible'] is False for x in ranked)
    fake[PROFILES[0].key]['closed']=30
    ranked=Tournament.rank(fake)
    selected=next(x for x in ranked if x['profile']==PROFILES[0].key)
    assert selected['rank_eligible'] is True


def test_crypto_transfer_is_asset_class_isolated():
    profile=next(p for p in PROFILES if p.key=='CRYPTO_CLEAN_PATH_10X_STOP6')
    data=frame()
    signal,reason=evaluate_profile(MARKETS['SILVER'],profile,data,data,data,129.9,130.0,{'crypto_breadth':{'long_allowed':True,'short_allowed':True}})
    assert signal is None
    assert reason=='NOT_APPLICABLE_TO_ASSET_CLASS'


def test_crypto_breadth_governor_pauses_opposite_direction():
    tickers=[]
    for i in range(100):
        tickers.append({'symbol':f'COIN{i}USDT','quoteVolume':str(1_000_000-i),'priceChangePercent':'3.0'})
    tickers.extend({'symbol':s,'quoteVolume':'5000000','priceChangePercent':'4.0'} for s in BinanceBreadthData.MAJORS)
    state=BinanceBreadthData.evaluate(tickers)
    assert state['long_allowed'] is True
    assert state['short_allowed'] is False
    assert state['pressure_score'] > 0


def test_v2_crypto_pair_uses_identical_high_precision_entry():
    m15=v2_frame(.03);h1=v2_frame(.10);h4=v2_frame(.18)
    bid=float(m15.iloc[-1].close);ask=bid+.01
    stop=next(p for p in PROFILES if p.key=='PREDICTION_V2_10X_STOP6')
    no_stop=next(p for p in PROFILES if p.key=='PREDICTION_V2_10X_NOSTOP')
    a,ra=evaluate_profile(MARKETS['BTC'],stop,m15,h1,h4,bid,ask,{'crypto_breadth':strong_long_breadth()})
    b,rb=evaluate_profile(MARKETS['BTC'],no_stop,m15,h1,h4,bid,ask,{'crypto_breadth':strong_long_breadth()})
    assert ra==rb=='ACCEPTED'
    assert a is not None and b is not None
    assert (a.direction,a.reference_price,a.score,a.setup)==(b.direction,b.reference_price,b.score,b.setup)


def test_v2_crypto_rejects_permissive_but_weak_transition_breadth():
    m15=v2_frame(.03);h1=v2_frame(.10);h4=v2_frame(.18)
    bid=float(m15.iloc[-1].close);ask=bid+.01
    profile=next(p for p in PROFILES if p.key=='PREDICTION_V2_10X_NOSTOP')
    breadth=strong_long_breadth(pressure=10)
    breadth['state']='CAUTION_TRANSITION'
    signal,reason=evaluate_profile(MARKETS['BTC'],profile,m15,h1,h4,bid,ask,{'crypto_breadth':breadth})
    assert signal is None
    assert reason=='V2_CRYPTO_PRESSURE_BELOW_18'


def test_v2_rejects_fresh_reversal_even_when_ema_structure_still_lags():
    m15=v2_frame(.03);h1=v2_frame(.10);h4=v2_frame(.18)
    base=float(h1.iloc[-4].close)
    for index,drop in zip(h1.index[-3:],[.2,.4,.6]):
        close=base-drop
        h1.loc[index,['open','high','low','close']]=[close+.05,close+.08,close-.04,close]
    bid=float(m15.iloc[-1].close);ask=bid+.01
    profile=next(p for p in PROFILES if p.key=='PREDICTION_V2_PRECISION')
    signal,reason=evaluate_profile(MARKETS['SILVER'],profile,m15,h1,h4,bid,ask)
    assert signal is None
    assert reason=='V2_HTF_MOMENTUM_OPPOSES_STRUCTURE'


def test_transferred_gold_and_crypto_lanes_can_admit_clean_forward_setup():
    def wave(step,amplitude):
        rows=[]
        for i in range(120):
            close=100+i*step+math.sin(i*.9)*amplitude
            rows.append({'time':pd.Timestamp('2026-01-01',tz='UTC')+pd.Timedelta(minutes=15*i),'open':close-.18,'high':close+.08,'low':close-.25,'close':close,'volume':100+(i%7)*5})
        data=pd.DataFrame(rows);i=data.index[-1]
        data.loc[i,'open']=data.loc[i,'close']-.30;data.loc[i,'low']=data.loc[i,'open']-.05;data.loc[i,'high']=data.loc[i,'close']+.04
        return data
    m15=wave(.03,.20);h1=wave(.10,.20);h4=wave(.18,.20);bid=float(m15.iloc[-1].close);ask=bid+.01
    gold=next(p for p in PROFILES if p.key=='GOLD_HTF_PRECISION')
    crypto=next(p for p in PROFILES if p.key=='CRYPTO_CLEAN_PATH_10X_STOP6')
    gold_signal,_=evaluate_profile(MARKETS['SILVER'],gold,m15,h1,h4,bid,ask)
    crypto_signal,_=evaluate_profile(MARKETS['BTC'],crypto,m15,h1,h4,bid,ask,{'crypto_breadth':{'state':'LONG_FAVORED','long_allowed':True,'short_allowed':False,'pressure_score':30}})
    assert gold_signal is not None
    assert crypto_signal is not None
    assert gold_signal.direction==crypto_signal.direction=='LONG'


def test_scanner_writes_running_heartbeat(tmp_path):
    import companion_runner

    class FakeData:
        async def candles(self, granularity, count):
            steps={'M15':.3,'H1':.5,'H4':1.0}
            return frame(100,step=steps[granularity])
        async def quote(self):
            return Quote(130.0,130.01,'2026-01-02T00:00:00+00:00')

    original_dir=companion_runner.DATA_DIR
    original_provider=companion_runner._provider
    try:
        companion_runner.DATA_DIR=tmp_path
        companion_runner._provider=lambda key,symbol:(FakeData(),'TEST_PROVIDER')
        result=asyncio.run(companion_runner.scan_one('SILVER'))
    finally:
        companion_runner.DATA_DIR=original_dir
        companion_runner._provider=original_provider
    assert result['state']=='RUNNING'
    assert result['ok'] is True
    assert result['profiles_evaluated']==len(PROFILES)
    assert result['prediction_engine']=='PREDICTION_V2_STRICT_CONFIRMATION'
    assert result['position_mark_interval_seconds'] <= result['scan_interval_seconds']
    assert result['leader'] is None
    assert result['scan_count'] >= 1
    assert result['last_scan_completed_at']
    assert (tmp_path/'silver_status.json').exists()
