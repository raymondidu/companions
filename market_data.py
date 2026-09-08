from __future__ import annotations
import time

import aiohttp
import pandas as pd
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


class CoinbaseData:
    """Public, credential-free BTC market data for paper research only."""

    SECONDS = {'M15': 900, 'H1': 3600, 'H4': 14400}

    def __init__(self, base_url: str, product: str = 'BTC-USD'):
        self.base = base_url.rstrip('/')
        self.product = product
        self.headers = {'Accept': 'application/json', 'User-Agent': 'tradehouse-companion-research/1.0'}

    async def _get(self, path, params=None):
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, headers=self.headers) as session:
            async with session.get(self.base + path, params=params) as response:
                body = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f'COINBASE {response.status}: {body[:300]}')
                return await response.json()

    async def _raw_candles(self, seconds: int):
        # Exchange candles support 15m and 1h. H4 is built from complete H1 bars.
        rows = await self._get(f'/products/{self.product}/candles', {'granularity': seconds})
        now = time.time()
        complete = [r for r in rows if len(r) >= 6 and float(r[0]) + seconds <= now]
        complete.sort(key=lambda r: float(r[0]))
        return pd.DataFrame([
            {
                'time': pd.to_datetime(float(r[0]), unit='s', utc=True),
                'low': float(r[1]), 'high': float(r[2]), 'open': float(r[3]),
                'close': float(r[4]), 'volume': float(r[5]),
            }
            for r in complete
        ])

    async def candles(self, granularity='M15', count=300):
        if granularity not in self.SECONDS:
            raise ValueError(f'Unsupported Coinbase granularity: {granularity}')
        if granularity != 'H4':
            frame = await self._raw_candles(self.SECONDS[granularity])
            if frame.empty:
                raise RuntimeError('No complete Coinbase candles returned.')
            return frame.tail(count).reset_index(drop=True)
        hourly = await self._raw_candles(self.SECONDS['H1'])
        if hourly.empty:
            raise RuntimeError('No complete Coinbase hourly candles returned.')
        four_hour = (
            hourly.set_index('time')
            .resample('4h', origin='epoch')
            .agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'})
            .dropna()
            .reset_index()
        )
        return four_hour.tail(count).reset_index(drop=True)

    async def quote(self):
        payload = await self._get(f'/products/{self.product}/ticker')
        return Quote(float(payload['bid']), float(payload['ask']), str(payload.get('time') or ''))
