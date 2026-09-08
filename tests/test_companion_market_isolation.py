from pathlib import Path

import pandas as pd

from companion_engine import CompanionSignal, CompanionStore, analyze
from companion_markets import MARKETS, isolation_contract

ROOT=Path(__file__).resolve().parents[1]


def _frame(n=80, start=100.0, step=0.2):
    rows=[]
    for i in range(n):
        c=start+i*step
        rows.append({'time':pd.Timestamp('2026-01-01',tz='UTC')+pd.Timedelta(minutes=15*i),'open':c-0.05,'high':c+0.25,'low':c-0.15,'close':c,'volume':100+i})
    return pd.DataFrame(rows)


def test_companion_contract_has_zero_live_authority():
    c=isolation_contract()
    assert c['paper_only'] is True
    assert c['live_authority'] is False
    assert c['tradehouse_delivery'] is False
    assert c['shares_gold_database'] is False
    assert c['shares_gold_wallet'] is False
    assert c['shares_gold_position_capacity'] is False
    assert c['can_modify_gold_signal'] is False
    assert c['copies_gold_thresholds'] is False
    assert set(c['markets']) == {'SILVER','USOIL','BTC'}
    assert all(v.live_authority is False and v.tradehouse_delivery is False for v in MARKETS.values())
    assert MARKETS['BTC'].provider_symbol == 'BTC-USD'


def test_companion_store_rejects_gold_database(tmp_path):
    try:
        CompanionStore(tmp_path/'gold.db',MARKETS['SILVER'])
        assert False
    except ValueError:
        pass


def test_each_market_uses_its_own_database(tmp_path):
    paths={}
    for key,cfg in MARKETS.items():
        p=tmp_path/f'{key.lower()}.db'; CompanionStore(p,cfg); paths[key]=p
    assert len({str(p) for p in paths.values()}) == 3


def test_paper_wallet_allows_only_one_open_position(tmp_path):
    cfg=MARKETS['SILVER']; s=CompanionStore(tmp_path/'silver.db',cfg)
    sig=CompanionSignal('SILVER','XAGUSD','LONG',80,'TREND_CONTINUATION',30.0,0.4,'2026-01-01T00:00:00+00:00',('TEST',))
    assert s.record(sig) is not None
    assert s.record(sig) is None


def test_crypto_stop_and_no_stop_lanes_are_independent(tmp_path):
    cfg=MARKETS['BTC']
    sig=CompanionSignal('BTC','BTCUSD','LONG',80,'TREND_CONTINUATION',100.0,1.0,'2026-01-01T00:00:00+00:00',('TEST',))
    stopped=CompanionStore(tmp_path/'stopped.db',cfg,{'execution_model':'CAPITAL_6_4_5X_STOP6','leverage':5,'paper_capital_usd':100})
    no_stop=CompanionStore(tmp_path/'no_stop.db',cfg,{'execution_model':'CAPITAL_6_4_5X_NOSTOP','leverage':5,'paper_capital_usd':100})
    stopped.record(sig);no_stop.record(sig)
    stopped.mark(98.7,98.71);no_stop.mark(98.7,98.71)
    assert stopped.summary()['closed']==1
    assert no_stop.summary()['open']==1
    no_stop.mark(101.3,101.31)
    assert no_stop.summary()['first_locks']==1
    assert no_stop.summary()['true_confidence']==1
    no_stop.mark(100.7,100.71)
    assert no_stop.summary()['closed']==1


def test_runtime_is_standalone_and_has_no_tradehouse_route():
    compose=(ROOT/'docker-compose.yml').read_text()
    runner=(ROOT/'companion_runner.py').read_text()
    assert 'image: companions-app' in compose
    assert 'gold-data' not in compose
    assert 'COMPANION_OANDA_TOKEN' in runner
    assert '/api/executor/' not in runner
    assert "'tradehouse_delivery': False" in runner


def test_container_repairs_only_the_dedicated_companion_volume():
    dockerfile=(ROOT/'Dockerfile').read_text()
    entrypoint=(ROOT/'docker-entrypoint.sh').read_text()
    assert 'ENTRYPOINT ["/app/docker-entrypoint.sh"]' in dockerfile
    assert 'chown -R appuser:appuser /app/companion-data' in entrypoint
    assert 'gold' not in entrypoint.lower()
