from __future__ import annotations

"""Standalone companion scanner process."""
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from companion_markets import MARKETS
from companion_exness_specs import CATALOG_SOURCE, CATALOG_VERIFIED_AT, validate_market_mapping
from companion_tournament import PROFILES, Tournament, evaluate_profile
from companion_tradehouse import ACTIVE_PATHS, deliver_selected_signal
from companion_learning import build_learning_report
from companion_counterfactual import infer_direction, update_counterfactuals
from market_data import BinanceBreadthData, CoinbaseData, OandaData

DATA_DIR = Path(os.getenv('COMPANION_DATA_DIR', '/app/companion-data'))
SCAN_SECONDS = max(30, int(os.getenv('COMPANION_SCAN_INTERVAL_SECONDS', '60')))
POSITION_MARK_SECONDS = max(5, int(os.getenv('COMPANION_POSITION_MARK_SECONDS', '10')))
STARTED_AT = datetime.now(timezone.utc).isoformat()
SCAN_COUNTS = {key: 0 for key in MARKETS}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, sort_keys=True, default=str))
    temporary.replace(path)


def _provider(key: str, symbol: str):
    if key == 'BTC':
        base = os.getenv('COMPANION_COINBASE_BASE_URL', 'https://api.exchange.coinbase.com').strip()
        return CoinbaseData(base, symbol), 'COINBASE_PUBLIC'
    base = os.getenv('COMPANION_OANDA_BASE_URL', '').strip()
    token = os.getenv('COMPANION_OANDA_TOKEN', '').strip()
    account = os.getenv('COMPANION_OANDA_ACCOUNT_ID', '').strip()
    if not (base and token and account):
        return None, 'OANDA_CREDENTIALS_MISSING'
    return OandaData(base, token, account, symbol), 'OANDA'


def _provider_symbol(key: str, configured: str | None) -> str | None:
    override = os.getenv(f'COMPANION_{key}_PROVIDER_SYMBOL', '').strip()
    return override or configured


def _exness_tradability(key: str, broker_symbol: str) -> dict:
    spec = validate_market_mapping(key, broker_symbol)
    configured = {
        item.strip().upper()
        for item in os.getenv('COMPANION_EXNESS_ACCOUNT_SYMBOLS', '').split(',')
        if item.strip()
    }
    account_status = 'UNVERIFIED_ON_ACCOUNT'
    if configured:
        account_status = 'VERIFIED_ON_ACCOUNT' if spec.broker_symbol.upper() in configured else 'NOT_ENABLED_ON_ACCOUNT'
    return {
        'venue': 'EXNESS',
        'symbol': spec.broker_symbol,
        'catalog_supported': True,
        'catalog_source': CATALOG_SOURCE,
        'catalog_verified_at': CATALOG_VERIFIED_AT,
        'account_status': account_status,
        'contract_size': spec.contract_size,
        'minimum_lot': spec.min_lot,
        'lot_step': spec.lot_step,
    }


def _live_profile(key: str):
    wanted = ACTIVE_PATHS.get(key)
    if not wanted:
        return None
    return next((p for p in PROFILES if p.key == wanted), None)


def _setup_key(m15) -> str:
    if m15 is None or len(m15) == 0:
        return 'UNKNOWN_SETUP'
    raw = m15.iloc[-1].get('time')
    return str(raw)


async def scan_one(key: str) -> dict:
    started = time.monotonic()
    scan_started_at = _utcnow()
    SCAN_COUNTS[key] += 1
    cfg = MARKETS[key]
    symbol = _provider_symbol(key, cfg.provider_symbol)
    status_path = DATA_DIR / f'{key.lower()}_status.json'
    out = {
        'market': key, 'broker_symbol': cfg.broker_symbol, 'paper_only': True,
        'live_authority': False, 'tradehouse_delivery': False,
        'provider_symbol': symbol, 'ok': False,
        'forward_only': True, 'tournament': True,
        'scanner_started_at': STARTED_AT,
        'last_scan_started_at': scan_started_at,
        'scan_count': SCAN_COUNTS[key],
        'scan_interval_seconds': SCAN_SECONDS,
        'position_mark_interval_seconds': POSITION_MARK_SECONDS,
        'prediction_engine': 'PREDICTION_V2_STRICT_CONFIRMATION',
        'legacy_paths_preserved_as_controls': True,
        'deploy_sha': os.getenv('COMPANION_DEPLOY_SHA', 'UNKNOWN'),
    }
    try:
        out['exness_tradability'] = _exness_tradability(key, cfg.broker_symbol)
        if out['exness_tradability']['account_status'] == 'NOT_ENABLED_ON_ACCOUNT':
            out.update(state='EXNESS_INSTRUMENT_NOT_ENABLED_ON_ACCOUNT')
            return out
        if not cfg.enabled_for_paper:
            out.update(state='DISABLED'); return out
        if not symbol:
            out.update(state='DATA_PROVIDER_UNCONFIGURED'); return out
        data, provider_name = _provider(key, symbol)
        out['provider'] = provider_name
        if data is None:
            out.update(state='COMPANION_DATA_PROVIDER_CREDENTIALS_MISSING'); return out
        m15,h1,h4,q = await asyncio.gather(
            data.candles('M15',300),data.candles('H1',300),data.candles('H4',300),data.quote()
        )
        context={}; warnings=[]
        if key=='BTC':
            try:
                breadth=await BinanceBreadthData(os.getenv('COMPANION_BINANCE_BASE_URL','https://api.binance.com')).snapshot()
                context['crypto_breadth']=breadth
            except Exception as exc:
                warnings.append(f'CRYPTO_BREADTH_UNAVAILABLE: {type(exc).__name__}: {exc}')

        tournament=Tournament(DATA_DIR/'tournament',cfg)
        profiles=tournament.step(m15,h1,h4,q.bid,q.ask,context)
        ranking=tournament.rank(profiles)
        leader=next((row for row in ranking if row.get('rank_eligible')),None)

        live_candidate=None
        live_gate='REFUSED_INSTRUMENT'
        live_profile=_live_profile(key)
        if live_profile is not None:
            candidate,live_gate=evaluate_profile(cfg,live_profile,m15,h1,h4,q.bid,q.ask,context)
            live_candidate=vars(candidate) if candidate is not None else None
        setup_key=_setup_key(m15)
        delivery=await deliver_selected_signal(
            key, profiles, live_candidate=live_candidate,
            setup_key=setup_key, live_gate=live_gate,
        )

        live_path=ACTIVE_PATHS.get(key)
        champion_row=next((r for r in ranking if r.get('profile')==live_path),None)
        if champion_row is not None and not champion_row.get('contract_size'):
            champion_row=dict(champion_row)
            champion_row['contract_size']=out['exness_tradability'].get('contract_size')
        shadow_direction=(live_candidate or {}).get('direction') if live_candidate else infer_direction(m15,h1,h4)
        counterfactual=update_counterfactuals(
            DATA_DIR,key,setup_key,live_gate,shadow_direction,
            q.bid,q.ask,champion_row,SCAN_COUNTS[key],
        )

        learning=build_learning_report(
            DATA_DIR,
            key,
            ranking,
            live_gate,
            delivery,
            context,
            SCAN_COUNTS[key],
        )
        learning['counterfactual']=counterfactual
        if counterfactual.get('resolved_shadows'):
            protected=int(counterfactual.get('protected_losers') or 0)
            blocked=int(counterfactual.get('blocked_winners') or 0)
            learning.setdefault('observations',[]).append(
                f"Rejected-signal shadows resolved: {protected} protected losers, {blocked} blocked +15% opportunities."
            )
            for gate,stats in (counterfactual.get('by_gate') or {}).items():
                decisive=int(stats.get('protected_losers') or 0)+int(stats.get('blocked_winners') or 0)
                if decisive>=20 and (stats.get('blocked_winner_rate_pct') or 0)>=60:
                    learning.setdefault('recommended_actions',[]).append(
                        f"{gate} blocked profitable shadows {stats['blocked_winner_rate_pct']}% of decisive cases ({decisive} sample); investigate a paper-only challenger with a controlled relaxation, not a live gate change."
                    )
                if decisive>=20 and (stats.get('protect_rate_pct') or 0)>=70:
                    learning.setdefault('observations',[]).append(
                        f"{gate} is currently protective: {stats['protect_rate_pct']}% of decisive shadow outcomes reached -10% adverse before +15%."
                    )

        out.update(
            ok=True,state='RUNNING',
            quote={'bid':q.bid,'ask':q.ask,'time':q.time},
            data={'m15_candles':len(m15),'h1_candles':len(h1),'h4_candles':len(h4)},
            profiles_evaluated=len(profiles),
            research_context=context,
            warnings=warnings,
            profiles=profiles,ranking=ranking,leader=leader,
            live_signal_candidate=live_candidate,
            live_signal_gate=live_gate,
            tradehouse_delivery=delivery,
            counterfactual=counterfactual,
            learning=learning,
            promotion_policy={
                'minimum_rank_sample':30,
                'minimum_resolved_trades_for_ranking':30,
                'minimum_resolved_trades_for_promotion':200,
                'minimum_first_lock_rate_pct':80,
                'maximum_worst_adverse_atr':2.0,
                'automatic_live_promotion':False,
                'requires_explicit_human_review':True,
            },
        )
    except Exception as exc:
        out.update(state='ERROR',error=f'{type(exc).__name__}: {exc}')
    finally:
        out['last_scan_completed_at'] = _utcnow()
        out['scan_duration_ms'] = round((time.monotonic() - started) * 1000, 1)
        _write_json(status_path, out)
    return out


async def mark_open_positions(key: str) -> None:
    """Use quote-only updates between full scans to reduce stop/lock overshoot."""
    try:
        cfg=MARKETS[key]
        symbol=_provider_symbol(key,cfg.provider_symbol)
        data,_=_provider(key,symbol)
        if data is None:return
        tournament=Tournament(DATA_DIR/'tournament',cfg)
        stores=[tournament.store(profile) for profile in PROFILES]
        if not any(store.has_open() for store in stores):return
        q=await data.quote()
        for store in stores:
            if store.has_open():store.mark(q.bid,q.ask)
    except Exception:
        return


async def run_forever() -> None:
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    for key, cfg in MARKETS.items():
        _write_json(DATA_DIR/f'{key.lower()}_status.json', {
            'market': key, 'broker_symbol': cfg.broker_symbol,
            'state': 'STARTING', 'ok': False, 'paper_only': True,
            'live_authority': False, 'tradehouse_delivery': False,
            'scanner_started_at': STARTED_AT, 'scan_count': 0,
            'scan_interval_seconds': SCAN_SECONDS,
            'deploy_sha': os.getenv('COMPANION_DEPLOY_SHA', 'UNKNOWN'),
        })
    while True:
        await asyncio.gather(*(scan_one(k) for k in MARKETS))
        deadline=time.monotonic()+SCAN_SECONDS
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:break
            await asyncio.sleep(min(POSITION_MARK_SECONDS,remaining))
            await asyncio.gather(*(mark_open_positions(k) for k in MARKETS))


if __name__ == '__main__':
    asyncio.run(run_forever())
