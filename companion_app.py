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
    return HTMLResponse('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Companion Market Tournaments</title><style>body{font-family:system-ui;margin:24px;background:#101114;color:#eee}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:12px}.card{background:#191b20;border:1px solid #30343b;border-radius:14px;padding:16px}.exec{background:#15181d;border:1px solid #3d4652;border-radius:14px;padding:16px;margin-bottom:14px}.learn{background:#111821;border:1px solid #35516d;border-radius:12px;padding:12px;margin:12px 0}.good{color:#70e39a}.warn{color:#ffd36a}.bad{color:#ff7777}.muted{color:#aaa;font-size:13px}.row{padding:8px 0;border-top:1px solid #2a2d33;font-size:13px}.leader{font-size:18px;font-weight:700}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}.pill{display:inline-block;border:1px solid #3a414c;border-radius:999px;padding:3px 8px;margin:2px;font-size:12px}.learn ul{margin:6px 0 0 18px;padding:0}.learn li{margin:5px 0;font-size:13px}</style></head><body><h1>Companion Strategy Tournaments</h1><p><b>PAPER RESEARCH + FAIL-CLOSED TRADEHOUSE DELIVERY.</b> Only USOIL/BALANCED_CLEAN and BTC/GOLD_M30_LOCAL_STRUCTURE in EXNESS_SURVIVAL_V1 may be delivered. Silver and all other paths remain paper-only.</p><div id="exec"></div><div id="cards" class="grid"></div><script>
const e=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function latestSignal(th,market){let a=Object.values(th.signals||{}).filter(x=>x.market===market);a.sort((x,y)=>String(y.attempted_at||y.updated_at||'').localeCompare(String(x.attempted_at||x.updated_at||'')));return a[0]||null}
function lifecycle(th,s){if(!s)return null;return (th.callbacks||{})[s.signal_id]||null}
function lifecycleText(cb){if(!cb)return 'NO CALLBACK YET';let p=[];p.push(cb.lifecycle_state||cb.last_event||'CALLBACK');if(cb.broker_position_id)p.push('broker '+cb.broker_position_id);if(cb.actual_fill_price!=null)p.push('fill '+cb.actual_fill_price);if(cb.net_realized_pnl_usd!=null)p.push('realized $'+cb.net_realized_pnl_usd);return p.join(' · ')}
function list(items,cls=''){return (items||[]).length?`<ul class=${cls}>${items.map(x=>`<li>${e(x)}</li>`).join('')}</ul>`:'<span class=muted>None yet.</span>'}
function learningPanel(x){let l=x.learning||{},c=l.champion||{},r=l.research_leader||{};if(!l.version)return '<div class=learn><b>Learning Engine</b><p class=muted>Waiting for first post-deploy learning scan.</p></div>';let gates=Object.entries(l.gate_frequency||{}).slice(0,4).map(([k,v])=>`${k}: ${v}`);return `<div class=learn><div class=leader>Learning Engine · ${e(l.version)}</div><p class=muted>${e(l.scans_recorded)} scans retained · live path ${e(l.live_path||'PAPER ONLY')} · no automatic live strategy changes or gate loosening.</p>${c.profile?`<p><b>Champion:</b> ${e(c.profile)} · evidence ${e(c.evidence_score)} · ${e(c.closed)} resolved · realized $${e(c.realized_pnl_usd)} · first-lock ${e(c.first_lock_rate_pct)}% · true-confidence ${e(c.true_confidence_rate_pct)}% · worst ${e(c.worst_adverse_atr)} ATR</p>`:''}${r.profile?`<p><b>Research leader:</b> ${e(r.profile)} · evidence ${e(r.evidence_score)} · ${e(r.closed)} resolved.</p>`:''}<p class=muted><b>Gate history:</b> ${e(gates.join(' · ')||'collecting')}</p><b>What the system learned</b>${list(l.observations)}<b>Recommended next actions</b>${list(l.recommended_actions)}${(l.warnings||[]).length?`<b class=warn>Warnings</b>${list(l.warnings)}`:''}${(l.promotion_candidates||[]).length?`<p class=warn><b>Promotion review candidates:</b> ${e(l.promotion_candidates.map(p=>p.profile).join(', '))}</p>`:''}</div>`}
async function load(){try{let r=await fetch('/api/markets',{cache:'no-store'}),d=await r.json(),th=d.tradehouse||{},sum=th.summary||{};document.getElementById('exec').innerHTML=`<div class=exec><div class=leader>TradeHouse Live Execution Truth</div><p><span class=pill>Executor ${th.executor_configured?'CONFIGURED':'NOT CONFIGURED'}</span><span class=pill>Pilot ${th.live_enable_requested?'ENABLED':'NOT REQUESTED'}</span><span class=pill>Candidates Generated ${e(sum.generated||0)}</span><span class=pill>Sent ${e(sum.sent||0)}</span><span class=pill>Accepted ${e(sum.accepted||0)}</span><span class=pill>Opened ${e(sum.opened||0)}</span><span class=pill>Closed ${e(sum.closed||0)}</span><span class=pill>Open failed ${e(sum.open_failed||0)}</span></p><p class=muted>Policy ${e(th.policy_version)} · cohort ${e(th.cohort)}. Generated means candidate generation, not a live send. HTTP 200/202 is receipt/acceptance only. A trade is real only after OPENED callback with broker_position_id.</p></div>`;let h='';for(const [k,x] of Object.entries(d.markets||{})){let rows=(x.ranking||[]).map((p,i)=>{let size=p.paper_lot_size?`${e(p.paper_lot_size)} lot / $${e(p.starting_equity_usd)} wallet`:`$${e(p.paper_trade_usd)} margin @ ${e(p.leverage)}x`;return `<div class=row>${i+1}. <b>${e(p.profile)}</b> [${e(p.family)}] · ${e(p.execution_model)} · ${size}<br>opened/resolved ${e(p.opened)}/${e(p.closed)} · open P&amp;L $${e(p.open_pnl_usd)} · realized P&amp;L $${e(p.realized_pnl_usd)} · first-lock ${e(p.first_lock_rate_pct)}% · true-confidence ${e(p.true_confidence_rate_pct)}% · worst ${e(p.worst_adverse_atr)} ATR / ${e(p.worst_adverse_capital_pct)}% capital · score ${e(p.research_score)}<br><span class=muted>latest gate: ${e(p.last_decision)}</span> ${p.promotion_ready?'· PROMOTION EVIDENCE READY':''}</div>`}).join('');let l=x.leader||{},cls=x.state==='RUNNING'?'good':(x.state==='ERROR'||x.state==='SCANNER_STALE'?'bad':'warn'),data=x.data||{},breadth=(x.research_context||{}).crypto_breadth,xt=x.exness_tradability||{},del=x.tradehouse_delivery||{},sig=latestSignal(th,k),cb=lifecycle(th,sig),cand=x.live_signal_candidate||null;let liveLine=k==='SILVER'?`REFUSED_INSTRUMENT · PAPER ONLY`:`${e(del.status||del.lifecycle_state||'NO DELIVERY')} ${del.signal_id?`· ${e(del.signal_id)}`:''}`;let transport=sig?`<div class=row><b>Latest TradeHouse transport</b><br><span class=mono>${e(sig.signal_id)}</span><br>path ${e(sig.path)} · ${e(sig.payload?.direction)} ${e(sig.payload?.instrument)} · entry ${e(sig.payload?.entry)} · created ${e(sig.payload?.signal_created_at)}<br>sent ${e(sig.attempted_at)} · HTTP ${e(sig.http_status)} · accepted ${sig.ack?.accepted===true?'YES':'NO/UNKNOWN'} · ack ${e(sig.ack?.status||sig.ack?.reason_code||sig.status||'—')}<br>callback: <b>${e(lifecycleText(cb))}</b></div>`:`<div class=row><b>Latest TradeHouse transport</b><br>No signal has been sent for this instrument yet.</div>`;let liveLeader=(th.summary?.opened||0)>0?e(l.profile||'NO LIVE LEADER YET'):'NO LIVE TRADEHOUSE TRADES YET';h+=`<div class=card><h2>${e(k)}</h2><div class=${cls}>${e(x.state)}</div><p>${e(x.broker_symbol)} · data: ${e(x.provider)}</p><p class=muted>Exness execution check: ${e(xt.symbol)} · catalogue ${xt.catalog_supported?'SUPPORTED':'UNVERIFIED'} · account ${e(xt.account_status)}</p><p><b>Live signal gate:</b> ${e(x.live_signal_gate||'NOT EXECUTABLE')} ${cand?`· candidate ${e(cand.direction)} @ ${e(cand.reference_price)}`:''}</p><p><b>TradeHouse:</b> ${liveLine}</p>${transport}<div class=leader>Live TradeHouse Leader: ${liveLeader}</div><p class=muted>Paper tournament ranking is shown below and is separate from live TradeHouse evidence.</p>${learningPanel(x)}<p class=muted>Scans: ${e(x.scan_count)} · last completed: ${e(x.last_scan_completed_at)} · age: ${e(x.heartbeat_age_seconds)}s · duration: ${e(x.scan_duration_ms)}ms</p><p class=muted>Data bars M15/H1/H4: ${e(data.m15_candles)}/${e(data.h1_candles)}/${e(data.h4_candles)} · profiles evaluated: ${e(x.profiles_evaluated)}</p>${breadth?`<p class=muted>Crypto market governor: ${e(breadth.state)} · pressure ${e(breadth.pressure_score)} · breadth up/down ${e(breadth.breadth_up_pct)}%/${e(breadth.breadth_down_pct)}%</p>`:''}${x.error?`<p class=bad>${e(x.error)}</p>`:''}${rows||'<p class=muted>No forward trades yet. Scanner health above shows whether this means no eligible setup or an engine fault.</p>'}</div>`}document.getElementById('cards').innerHTML=h}catch(err){document.getElementById('cards').innerHTML=`<div class=card><div class=bad>DASHBOARD API ERROR</div><p>${e(err)}</p></div>`}}load();setInterval(load,15000)</script></body></html>''')
