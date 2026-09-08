from __future__ import annotations
import aiohttp, pandas as pd
from dataclasses import dataclass

GRANULARITY={'S5':'S5','M1':'M1','M5':'M5','M15':'M15','H1':'H1','H4':'H4','D':'D'}
@dataclass
class Quote: bid: float; ask: float; time: str
class OandaData:
    def __init__(self, base_url:str, token:str, account_id:str, instrument:str):
        self.base=base_url.rstrip('/'); self.token=token; self.account_id=account_id; self.instrument=instrument
        self.headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'}
    async def _get(self, path, params=None):
        timeout=aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, headers=self.headers) as s:
            async with s.get(self.base+path, params=params) as r:
                text=await r.text()
                if r.status>=400: raise RuntimeError(f'OANDA {r.status}: {text[:300]}')
                return await r.json()
    async def candles(self, granularity='M15', count=300):
        j=await self._get(f'/v3/instruments/{self.instrument}/candles', {'price':'M','granularity':GRANULARITY[granularity],'count':count})
        rows=[]
        for c in j.get('candles',[]):
            if not c.get('complete'): continue
            m=c['mid']; rows.append({'time':pd.to_datetime(c['time'],utc=True),'open':float(m['o']),'high':float(m['h']),'low':float(m['l']),'close':float(m['c']),'volume':float(c.get('volume',0))})
        if not rows: raise RuntimeError('No complete OANDA candles returned.')
        return pd.DataFrame(rows)
    async def quote(self):
        j=await self._get(f'/v3/accounts/{self.account_id}/pricing', {'instruments':self.instrument})
        p=j['prices'][0]
        return Quote(float(p['bids'][0]['price']),float(p['asks'][0]['price']),p['time'])
