from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from companion_markets import MARKETS, isolation_contract

DATA_DIR=Path(os.getenv('COMPANION_DATA_DIR','/app/companion-data'))
app=FastAPI(title='Isolated Companion Markets Research')


def _status(key: str) -> dict:
    p=DATA_DIR/f'{key.lower()}_status.json'
    if not p.exists():
        return {'market':key,'state':'NOT_STARTED','paper_only':True,'live_authority':False,'tradehouse_delivery':False}
    try:return json.loads(p.read_text())
    except Exception as exc:return {'market':key,'state':'STATUS_READ_ERROR','error':f'{type(exc).__name__}: {exc}','paper_only':True,'live_authority':False,'tradehouse_delivery':False}


@app.get('/health')
def health():
    states={k:_status(k) for k in MARKETS}
    return {'ok':True,'service':'COMPANION_RESEARCH_ONLY','isolation':isolation_contract(),'markets':states}


@app.get('/api/isolation-contract')
def isolation(): return isolation_contract()


@app.get('/api/markets')
def markets():
    return {'ok':True,'paper_only':True,'live_authority':False,'tradehouse_delivery':False,'markets':{k:_status(k) for k in MARKETS}}


@app.get('/',response_class=HTMLResponse)
def dashboard():
    return HTMLResponse('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Companion Market Tournaments</title><style>body{font-family:system-ui;margin:24px;background:#101114;color:#eee}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}.card{background:#191b20;border:1px solid #30343b;border-radius:14px;padding:16px}.good{color:#70e39a}.warn{color:#ffd36a}.muted{color:#aaa;font-size:13px}.row{padding:7px 0;border-top:1px solid #2a2d33}.leader{font-size:18px;font-weight:700}</style></head><body><h1>Companion Strategy Tournaments</h1><p><b>PAPER ONLY · FORWARD ONLY.</b> Silver, Oil and BTC are physically and logically separated from Gold. No live promotion is automatic.</p><div id="cards" class="grid"></div><script>const e=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));async function load(){let r=await fetch('/api/markets',{cache:'no-store'}),d=await r.json(),h='';for(const [k,x] of Object.entries(d.markets||{})){let rows=(x.ranking||[]).map((p,i)=>`<div class=row>${i+1}. <b>${e(p.profile)}</b> · opened ${e(p.opened)} · locks ${e(p.first_lock_rate_pct)}% · worst ${e(p.worst_adverse_atr)} ATR · score ${e(p.research_score)} ${p.promotion_ready?'· PROMOTION EVIDENCE READY':''}</div>`).join('');let l=x.leader||{};h+=`<div class=card><h2>${e(k)}</h2><div class=${x.state==='RUNNING'?'good':'warn'}>${e(x.state)}</div><p>${e(x.broker_symbol)}</p><div class=leader>Leader: ${e(l.profile||'NO EVIDENCE YET')}</div>${rows||'<p class=muted>No forward trades yet.</p>'}<p class=muted>Minimum evidence: 30 trades · ≥80% first-lock · worst adverse ≤2 ATR. Human review still required. Live authority: NO · TradeHouse: NO.</p></div>`}document.getElementById('cards').innerHTML=h}load();setInterval(load,15000)</script></body></html>''')
