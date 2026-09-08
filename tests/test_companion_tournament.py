import pandas as pd
from companion_markets import MARKETS
from companion_tournament import PROFILES, Tournament, candidate_for_profile


def frame(n=100,start=100.0,step=0.3):
    rows=[]
    for i in range(n):
        c=start+i*step
        rows.append({'time':pd.Timestamp('2026-01-01',tz='UTC')+pd.Timedelta(minutes=15*i),'open':c-.05,'high':c+.30,'low':c-.15,'close':c,'volume':100+i})
    return pd.DataFrame(rows)


def test_tournament_has_multiple_independent_profiles():
    assert len(PROFILES) >= 5
    assert len({p.key for p in PROFILES}) == len(PROFILES)


def test_each_profile_gets_its_own_wallet_db(tmp_path):
    t=Tournament(tmp_path,MARKETS['SILVER'])
    stores=[t.store(p) for p in PROFILES]
    assert len({s.path for s in stores}) == len(PROFILES)
    assert all('gold.db' not in s.path.lower() for s in stores)


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
