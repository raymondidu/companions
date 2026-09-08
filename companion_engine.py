from __future__ import annotations

"""Standalone paper engine for Silver/Oil/BTC companion research.

Design rule: this module never imports Gold admission, Gold execution, TradeHouse
transport, Gold history, Gold callbacks, or Gold paper stores. It can be deleted or
crash without changing a Gold decision.
"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from companion_markets import CompanionMarket


@dataclass(frozen=True)
class CompanionSignal:
    market_key: str
    broker_symbol: str
    direction: str
    score: int
    setup: str
    reference_price: float
    entry_atr: float
    created_at: str
    reasons: tuple[str, ...]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    d = series.diff()
    gain = d.clip(lower=0).rolling(period).mean()
    loss = (-d.clip(upper=0)).rolling(period).mean()
    safe_loss = loss.where(loss != 0)
    rs = gain / safe_loss
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(loss != 0, 100.0)
    rsi = rsi.where(gain != 0, 0.0)
    both_flat = (gain == 0) & (loss == 0)
    return rsi.where(~both_flat, 50.0)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["ema20"] = x["close"].ewm(span=20, adjust=False).mean()
    x["ema50"] = x["close"].ewm(span=50, adjust=False).mean()
    x["rsi14"] = _rsi(x["close"], 14)
    prev = x["close"].shift(1)
    tr = pd.concat([(x["high"] - x["low"]).abs(), (x["high"] - prev).abs(), (x["low"] - prev).abs()], axis=1).max(axis=1)
    x["atr14"] = tr.rolling(14).mean()
    x["range_high20"] = x["high"].rolling(20).max().shift(1)
    x["range_low20"] = x["low"].rolling(20).min().shift(1)
    x["volume_ma20"] = x["volume"].rolling(20).mean()
    return x


def analyze(market: CompanionMarket, m15: pd.DataFrame, h1: pd.DataFrame, h4: pd.DataFrame, bid: float, ask: float) -> CompanionSignal | None:
    if min(len(m15), len(h1), len(h4)) < 60:
        return None
    a15, a1, a4 = enrich(m15), enrich(h1), enrich(h4)
    r15, r1, r4 = a15.iloc[-1], a1.iloc[-1], a4.iloc[-1]
    if pd.isna(r15.atr14) or float(r15.atr14) <= 0:
        return None
    long_trend = r4.ema20 > r4.ema50 and r1.ema20 > r1.ema50
    short_trend = r4.ema20 < r4.ema50 and r1.ema20 < r1.ema50
    long_momo = r15.close > r15.ema20 and 52 <= r15.rsi14 <= 78
    short_momo = r15.close < r15.ema20 and 22 <= r15.rsi14 <= 48
    long_break = pd.notna(r15.range_high20) and r15.close > r15.range_high20
    short_break = pd.notna(r15.range_low20) and r15.close < r15.range_low20
    vol_ok = bool(pd.notna(r15.volume_ma20) and r15.volume >= 0.85 * r15.volume_ma20)
    direction = None
    score = 0
    reasons: list[str] = []
    setup = "NONE"
    if long_trend:
        direction = "LONG"; score += 38; reasons.append("H1_H4_LONG_TREND")
        if long_momo: score += 22; reasons.append("M15_LONG_MOMENTUM"); setup = "TREND_CONTINUATION"
        if long_break: score += 24; reasons.append("M15_RANGE_BREAKOUT"); setup = "BREAKOUT_CONTINUATION"
    elif short_trend:
        direction = "SHORT"; score += 38; reasons.append("H1_H4_SHORT_TREND")
        if short_momo: score += 22; reasons.append("M15_SHORT_MOMENTUM"); setup = "TREND_CONTINUATION"
        if short_break: score += 24; reasons.append("M15_RANGE_BREAKOUT"); setup = "BREAKOUT_CONTINUATION"
    else:
        return None
    if vol_ok:
        score += 12; reasons.append("M15_VOLUME_OK")
    atr = float(r15.atr14)
    spread = max(0.0, float(ask) - float(bid))
    if spread / atr <= 0.18:
        score += 8; reasons.append("SPREAD_TO_ATR_OK")
    if score < 70 or setup == "NONE":
        return None
    ref = float(ask if direction == "LONG" else bid)
    return CompanionSignal(market.key, market.broker_symbol, direction, min(score, 100), setup, ref, atr, _utcnow(), tuple(reasons))


class CompanionStore:
    def __init__(self, path: str | Path, market: CompanionMarket):
        self.path = str(path)
        self.market = market
        if "gold.db" in self.path.lower():
            raise ValueError("Companion markets may not use the Gold database")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS companion_signals(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_key TEXT NOT NULL, broker_symbol TEXT NOT NULL, created_at TEXT NOT NULL,
                direction TEXT NOT NULL, score INTEGER NOT NULL, setup TEXT NOT NULL,
                reference_price REAL NOT NULL, entry_atr REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN', opened_at TEXT, closed_at TEXT,
                last_price REAL, price_return_pct REAL NOT NULL DEFAULT 0,
                max_favorable_atr REAL NOT NULL DEFAULT 0, max_adverse_atr REAL NOT NULL DEFAULT 0,
                first_lock_reached INTEGER NOT NULL DEFAULT 0, lock_level_atr REAL NOT NULL DEFAULT 0,
                exit_reason TEXT, reasons_json TEXT NOT NULL DEFAULT '[]'
            )""")
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30); c.row_factory = sqlite3.Row; return c
    def has_open(self) -> bool:
        with self.connect() as c:
            return c.execute("SELECT 1 FROM companion_signals WHERE status='OPEN' LIMIT 1").fetchone() is not None
    def record(self, sig: CompanionSignal) -> int | None:
        if self.has_open():
            return None
        with self.connect() as c:
            cur = c.execute("""INSERT INTO companion_signals(
                market_key,broker_symbol,created_at,direction,score,setup,reference_price,entry_atr,opened_at,last_price,reasons_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                sig.market_key,sig.broker_symbol,sig.created_at,sig.direction,sig.score,sig.setup,
                sig.reference_price,sig.entry_atr,sig.created_at,sig.reference_price,json.dumps(sig.reasons),
            ))
            return int(cur.lastrowid)
    def mark(self, bid: float, ask: float, high: float | None = None, low: float | None = None) -> None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM companion_signals WHERE status='OPEN' ORDER BY id DESC LIMIT 1").fetchone()
            if not row: return
            r = dict(row); entry=float(r['reference_price']); atr=max(float(r['entry_atr']),1e-12); direction=r['direction']
            market=float(bid if direction=='LONG' else ask); hi=float(high if high is not None else max(bid,ask)); lo=float(low if low is not None else min(bid,ask))
            if direction=='LONG':
                cur_atr=(market-entry)/atr; favorable=max(0.0,(hi-entry)/atr); adverse=max(0.0,(entry-lo)/atr); price_return=100*(market-entry)/entry
            else:
                cur_atr=(entry-market)/atr; favorable=max(0.0,(entry-lo)/atr); adverse=max(0.0,(hi-entry)/atr); price_return=100*(entry-market)/entry
            mf=max(float(r['max_favorable_atr'] or 0),favorable); ma=max(float(r['max_adverse_atr'] or 0),adverse)
            lock=float(r['lock_level_atr'] or 0); armed=bool(r['first_lock_reached'])
            if mf >= self.market.first_trigger_atr:
                stage=1+int((mf-self.market.first_trigger_atr+1e-12)//self.market.step_atr)
                lock=max(lock,self.market.first_lock_atr+(stage-1)*self.market.step_atr); armed=True
            should_close=armed and cur_atr <= lock
            c.execute("""UPDATE companion_signals SET last_price=?,price_return_pct=?,max_favorable_atr=?,max_adverse_atr=?,first_lock_reached=?,lock_level_atr=?,status=?,closed_at=?,exit_reason=? WHERE id=?""",(
                market,price_return,mf,ma,1 if armed else 0,lock,'CLOSED' if should_close else 'OPEN',_utcnow() if should_close else None,'PAPER_ATR_PROFIT_LOCK_EXIT' if should_close else None,r['id']))
    def summary(self) -> dict:
        with self.connect() as c: rows=[dict(x) for x in c.execute("SELECT * FROM companion_signals ORDER BY id DESC")]
        closed=[r for r in rows if r['status']=='CLOSED']; opened=len(rows); locks=sum(bool(r['first_lock_reached']) for r in rows)
        return {
            'market': self.market.key, 'broker_symbol': self.market.broker_symbol, 'paper_only': True,
            'live_authority': False, 'tradehouse_delivery': False, 'database_path': self.path,
            'policy_state': self.market.policy_state,
            'policy': {'first_trigger_atr':self.market.first_trigger_atr,'first_lock_atr':self.market.first_lock_atr,'step_atr':self.market.step_atr},
            'opened': opened, 'open': sum(r['status']=='OPEN' for r in rows), 'closed': len(closed),
            'first_locks': locks, 'first_lock_rate_pct': round(100*locks/opened,2) if opened else 0.0,
            'worst_adverse_atr': round(max([float(r['max_adverse_atr'] or 0) for r in rows] or [0]),2),
            'recent': rows[:20],
        }
