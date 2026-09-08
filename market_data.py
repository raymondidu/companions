from __future__ import annotations
import time
from statistics import median

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


class BinanceBreadthData:
    """Public crypto breadth used only to gate the BTC paper-transfer lanes."""

    STABLE_BASES = {'USDC','FDUSD','TUSD','USDP','DAI','EUR','BUSD'}
    MAJORS = ('BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT')

    def __init__(self, base_url: str = 'https://api.binance.com'):
        self.base = base_url.rstrip('/')

    async def snapshot(self):
        timeout=aiohttp.ClientTimeout(total=20)
        headers={'Accept':'application/json','User-Agent':'tradehouse-companion-research/1.0'}
        async with aiohttp.ClientSession(timeout=timeout,headers=headers) as session:
            async with session.get(self.base+'/api/v3/ticker/24hr') as response:
                body=await response.text()
                if response.status>=400:raise RuntimeError(f'BINANCE {response.status}: {body[:300]}')
                return self.evaluate(await response.json())

    @classmethod
    def evaluate(cls,tickers):
        rows=[]
        for ticker in tickers or []:
            symbol=str(ticker.get('symbol') or '')
            if not symbol.endswith('USDT') or symbol[:-4] in cls.STABLE_BASES:continue
            try:qv=float(ticker.get('quoteVolume') or 0);change=float(ticker.get('priceChangePercent') or 0)
            except Exception:continue
            if qv>0:rows.append((symbol,change,qv))
        if not rows:return {'state':'PAUSE_NO_MARKET_DATA','long_allowed':False,'short_allowed':False,'pressure_score':0,'core_assets':0}
        rows.sort(key=lambda x:x[2],reverse=True);core=rows[:200];total=sum(x[2] for x in core) or 1
        ups=[x for x in core if x[1]>.15];downs=[x for x in core if x[1]<-.15]
        up_volume=100*sum(x[2] for x in ups)/total;down_volume=100*sum(x[2] for x in downs)/total
        up_breadth=100*len(ups)/len(core);down_breadth=100*len(downs)/len(core);med=median([x[1] for x in core])
        by_symbol={s:c for s,c,_ in core};major_up=sum(1 for s in cls.MAJORS if by_symbol.get(s,0)>.20);major_down=sum(1 for s in cls.MAJORS if by_symbol.get(s,0)<-.20)
        pressure=max(-100,min(100,.35*(up_breadth-down_breadth)+.40*(up_volume-down_volume)+.15*((major_up-major_down)*8)+.03*max(-20,min(20,med*4))))
        severe_sell=down_volume>=68 and down_breadth>=60 and (major_down>=2 or med<=-1)
        severe_buy=up_volume>=68 and up_breadth>=60 and (major_up>=2 or med>=1)
        if severe_sell:state,long_allowed,short_allowed='SHORT_OK_LONG_PAUSE',False,True
        elif severe_buy:state,long_allowed,short_allowed='LONG_OK_SHORT_PAUSE',True,False
        elif pressure<=-18:state,long_allowed,short_allowed='SHORT_FAVORED',False,True
        elif pressure>=18:state,long_allowed,short_allowed='LONG_FAVORED',True,False
        elif abs(pressure)<8 and max(up_volume,down_volume)<58:state,long_allowed,short_allowed='CAUTION_MIXED',True,True
        else:state,long_allowed,short_allowed=('CAUTION_TRANSITION',pressure>=0,pressure<=0)
        return {'state':state,'long_allowed':long_allowed,'short_allowed':short_allowed,'pressure_score':round(pressure,2),'breadth_up_pct':round(up_breadth,2),'breadth_down_pct':round(down_breadth,2),'volume_up_pct':round(up_volume,2),'volume_down_pct':round(down_volume,2),'major_up':major_up,'major_down':major_down,'core_assets':len(core)}
