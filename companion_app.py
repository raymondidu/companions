from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from companion_markets import MARKETS, isolation_contract
from companion_tradehouse import delivery_snapshot, record_callback, verify_callback_signature

DATA_DIR=Path(os.getenv('COMPANION_DATA_DIR','/app/companion-data'))
app=FastAPI(title='Isolated Companion Markets Research')
SCAN_SECONDS=max(30,int(os.getenv('COMPANION_SCAN_INTERVAL_SECONDS','60')))


def _status(key: str) -> dict:
    p=DATA_DIR/f'{key.lower()}_status.json'
    if not p.exists():
        return {'market':key,'state':'NOT_STARTED','paper_only':True,'live_authority':False,'tradehouse_delivery':False}
    try:
        value=json.loads(p.read_text())
        completed=value.get('last_scan_completed_at')
        if completed:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(completed.replace('Z','+00:00'))).total_seconds()
            value['heartbeat_age_seconds']=round(max(0,age),1)
            value['heartbeat_stale']=age > max(180,SCAN_SECONDS*3)
            if value['heartbeat_stale']:
                value['reported_state']=value.get('state')
                value['state']='SCANNER_STALE'
                value['ok']=False
        return value
    except Exception as exc:return {'market':key,'state':'STATUS_READ_ERROR','error':f'{type(exc).__name__}: {exc}','paper_only':True,'live_authority':False,'tradehouse_delivery':False}


@app.get('/health')
def health():
    states={k:_status(k) for k in MARKETS}
    return {'ok':all(x.get('state')=='RUNNING' for x in states.values()),'service':'COMPANION_RESEARCH_WITH_TRADEHOUSE_DELIVERY','deploy_sha':os.getenv('COMPANION_DEPLOY_SHA','UNKNOWN'),'isolation':isolation_contract(),'markets':states,'tradehouse':delivery_snapshot()}


@app.get('/api/isolation-contract')
def isolation(): return isolation_contract()


@app.get('/api/markets')
def markets():
    return {'ok':True,'paper_only':True,'live_authority':False,'tradehouse_delivery':True,'markets':{k:_status(k) for k in MARKETS},'tradehouse':delivery_snapshot()}


@app.get('/api/companion/tradehouse')
def tradehouse_state():
    return delivery_snapshot()


@app.post('/api/companion/callback')
async def tradehouse_callback(request: Request):
    body=await request.body()
    if not verify_callback_signature(body,request.headers):
        raise HTTPException(status_code=401,detail='UNAUTHORIZED_CALLBACK')
    try:
        payload=json.loads(body.decode('utf-8'))
    except Exception:
        raise HTTPException(status_code=400,detail='INVALID_JSON')
    ok,reason=record_callback(payload)
    if not ok:
        raise HTTPException(status_code=400,detail=reason)
    return {'received':True,'accepted':True,'signal_id':payload.get('signal_id'),'event_id':payload.get('event_id'),'status':reason}


@app.get('/',response_class=HTMLResponse)
def dashboard():
    return HTMLResponse('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Companion Market Tournaments</title><style>body{font-family:system-ui;margin:24px;background:#101114;color:#eee}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:12px}.card{background:#191b20;border:1px solid #30343b;border-radius:14px;padding:16px}.good{color:#70e39a}.warn{color:#ffd36a}.bad{color:#ff7777}.muted{color:#aaa;font-size:13px}.row{padding:8px 0;border-top:1px solid #2a2d33;font-size:13px}.leader{font-size:18px;font-weight:700}</style></head><body><h1>Companion Strategy Tournaments</h1><p><b>PAPER RESEARCH + FAIL-CLOSED TRADEHOUSE DELIVERY.</b> Only USOIL/BALANCED_CLEAN and BTC/GOLD_M30_LOCAL_STRUCTURE in EXNESS_SURVIVAL_V1 may be delivered. Silver and all other paths remain paper-only.</p><div id="cards" class="grid"></div><script>const e=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));async function load(){try{let r=await fetch('/api/markets',{cache:'no-store'}),d=await r.json(),h='';for(const [k,x] of Object.entries(d.markets||{})){let rows=(x.ranking||[]).map((p,i)=>{let size=p.paper_lot_size?`${e(p.paper_lot_size)} lot / $${e(p.starting_equity_usd)} wallet`:`$${e(p.paper_trade_usd)} margin @ ${e(p.leverage)}x`;return `<div class=row>${i+1}. <b>${e(p.profile)}</b> [${e(p.family)}] · ${e(p.execution_model)} · ${size}<br>opened/resolved ${e(p.opened)}/${e(p.closed)} · open P&amp;L $${e(p.open_pnl_usd)} · realized P&amp;L $${e(p.realized_pnl_usd)} · first-lock ${e(p.first_lock_rate_pct)}% · true-confidence ${e(p.true_confidence_rate_pct)}% · worst ${e(p.worst_adverse_atr)} ATR / ${e(p.worst_adverse_capital_pct)}% capital · score ${e(p.research_score)}<br><span class=muted>latest gate: ${e(p.last_decision)}</span> ${p.promotion_ready?'· PROMOTION EVIDENCE READY':''}</div>`}).join('');let l=x.leader||{},cls=x.state==='RUNNING'?'good':(x.state==='ERROR'||x.state==='SCANNER_STALE'?'bad':'warn'),data=x.data||{},breadth=(x.research_context||{}).crypto_breadth,xt=x.exness_tradability||{},th=x.tradehouse_delivery||{};h+=`<div class=card><h2>${e(k)}</h2><div class=${cls}>${e(x.state)}</div><p>${e(x.broker_symbol)} · data: ${e(x.provider)}</p><p class=muted>Exness execution check: ${e(xt.symbol)} · catalogue ${xt.catalog_supported?'SUPPORTED':'UNVERIFIED'} · account ${e(xt.account_status)}</p><p class=muted>TradeHouse: ${e(th.status||th.lifecycle_state||'NO DELIVERY')} ${th.signal_id?`· ${e(th.signal_id)}`:''}</p><div class=leader>Leader: ${e(l.profile||'NO EVIDENCE YET')}</div><p class=muted>Scans: ${e(x.scan_count)} · last completed: ${e(x.last_scan_completed_at)} · age: ${e(x.heartbeat_age_seconds)}s · duration: ${e(x.scan_duration_ms)}ms</p><p class=muted>Data bars M15/H1/H4: ${e(data.m15_candles)}/${e(data.h1_candles)}/${e(data.h4_candles)} · profiles evaluated: ${e(x.profiles_evaluated)}</p>${breadth?`<p class=muted>Crypto market governor: ${e(breadth.state)} · pressure ${e(breadth.pressure_score)} · breadth up/down ${e(breadth.breadth_up_pct)}%/${e(breadth.breadth_down_pct)}%</p>`:''}${x.error?`<p class=bad>${e(x.error)}</p>`:''}${rows||'<p class=muted>No forward trades yet. Scanner health above shows whether this means no eligible setup or an engine fault.</p>'}</div>`}document.getElementById('cards').innerHTML=h}catch(err){document.getElementById('cards').innerHTML=`<div class=card><div class=bad>DASHBOARD API ERROR</div><p>${e(err)}</p></div>`}}load();setInterval(load,15000)</script></body></html>''')
