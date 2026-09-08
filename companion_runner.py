from __future__ import annotations

"""Standalone companion scanner process."""
import asyncio
import json
import os
from pathlib import Path

from companion_markets import MARKETS
from companion_tournament import Tournament
from market_data import OandaData

DATA_DIR = Path(os.getenv('COMPANION_DATA_DIR', '/app/companion-data'))
SCAN_SECONDS = max(30, int(os.getenv('COMPANION_SCAN_INTERVAL_SECONDS', '60')))


def _provider_symbol(key: str, configured: str | None) -> str | None:
    override = os.getenv(f'COMPANION_{key}_PROVIDER_SYMBOL', '').strip()
    return override or configured


async def scan_one(key: str) -> dict:
    cfg = MARKETS[key]
    symbol = _provider_symbol(key, cfg.provider_symbol)
    status_path = DATA_DIR / f'{key.lower()}_status.json'
    out = {
        'market': key, 'broker_symbol': cfg.broker_symbol, 'paper_only': True,
        'live_authority': False, 'tradehouse_delivery': False,
        'provider_symbol': symbol, 'ok': False,
        'forward_only': True, 'tournament': True,
    }
    try:
        if not cfg.enabled_for_paper:
            out.update(state='DISABLED'); return out
        if not symbol:
            out.update(state='DATA_PROVIDER_UNCONFIGURED'); return out
        base=os.getenv('COMPANION_OANDA_BASE_URL','').strip()
        token=os.getenv('COMPANION_OANDA_TOKEN','').strip()
        account=os.getenv('COMPANION_OANDA_ACCOUNT_ID','').strip()
        if not (base and token and account):
            out.update(state='COMPANION_DATA_PROVIDER_CREDENTIALS_MISSING'); return out
        data=OandaData(base,token,account,symbol)
        m15,h1,h4,q = await asyncio.gather(
            data.candles('M15',300),data.candles('H1',300),data.candles('H4',300),data.quote()
        )
        tournament=Tournament(DATA_DIR/'tournament',cfg)
        profiles=tournament.step(m15,h1,h4,q.bid,q.ask)
        ranking=tournament.rank(profiles)
        leader=ranking[0] if ranking else None
        out.update(
            ok=True,state='RUNNING',
            quote={'bid':q.bid,'ask':q.ask,'time':q.time},
            profiles=profiles,ranking=ranking,leader=leader,
            promotion_policy={
                'minimum_opened_trades':30,
                'minimum_first_lock_rate_pct':80,
                'maximum_worst_adverse_atr':2.0,
                'automatic_live_promotion':False,
                'requires_explicit_human_review':True,
            },
        )
    except Exception as exc:
        out.update(state='ERROR',error=f'{type(exc).__name__}: {exc}')
    finally:
        DATA_DIR.mkdir(parents=True,exist_ok=True)
        status_path.write_text(json.dumps(out,sort_keys=True,default=str))
    return out


async def run_forever() -> None:
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    while True:
        await asyncio.gather(*(scan_one(k) for k in MARKETS))
        await asyncio.sleep(SCAN_SECONDS)


if __name__ == '__main__':
    asyncio.run(run_forever())
