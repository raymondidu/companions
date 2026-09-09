from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

POLICY_VERSION = "COMPANION_OILBTC_15LOCK10_STEP10_V1"
COHORT = "EXNESS_SURVIVAL_V1"
ACTIVE_PATHS = {
    "USOIL": "BALANCED_CLEAN",
    "BTC": "GOLD_M30_LOCAL_STRUCTURE",
}
EXECUTOR_INSTRUMENT = {"USOIL": "USOIL", "BTC": "BTCUSD"}
CALLBACK_EVENTS = {
    "RECEIVED", "OPENING", "OPENED", "POSITION_UPDATE", "PROTECTION_ARMED",
    "PROTECTION_RAISED", "CLOSE_REQUESTED", "CLOSED", "OPEN_FAILED",
    "POSITION_LOST", "RECOVERED_AFTER_RESTART",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_env(*names: str) -> str:
    """Return the first non-empty configured environment value.

    Companion-specific names remain preferred, but existing Gold/TradeHouse
    deployments can be reused without duplicating secrets on the host.
    """
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _executor_base_url() -> str:
    return _first_env(
        "COMPANION_EXECUTOR_BASE_URL",
        "TRADEHOUSE_EXECUTOR_BASE_URL",
        "GOLD_EXECUTOR_BASE_URL",
        "EXECUTOR_BASE_URL",
    ).rstrip("/")


def _executor_secret() -> str:
    return _first_env(
        "COMPANION_EXECUTOR_SECRET",
        "TRADEHOUSE_EXECUTOR_SECRET",
        "GOLD_EXECUTOR_SECRET",
        "EXECUTOR_SECRET",
    )


def _data_dir() -> Path:
    return Path(os.getenv("COMPANION_DATA_DIR", "/app/companion-data"))


def _state_path() -> Path:
    return _data_dir() / "tradehouse_delivery.json"


def _callback_path() -> Path:
    return _data_dir() / "tradehouse_callbacks.json"


def _read(path: Path) -> dict:
    try:
        if path.exists():
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
    except Exception:
        pass
    return {}


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, sort_keys=True, default=str))
    tmp.replace(path)


def delivery_snapshot() -> dict:
    state = _read(_state_path())
    callbacks = _read(_callback_path())
    signals = state.get("signals", {})
    callback_signals = callbacks.get("signals", {})
    summary = {
        "generated": len(signals),
        "sent": sum(1 for x in signals.values() if x.get("attempted_at")),
        "accepted": sum(1 for x in signals.values() if x.get("http_status") in (200, 202) and isinstance(x.get("ack"), dict) and x["ack"].get("accepted")),
        "opened": sum(1 for x in callback_signals.values() if x.get("broker_position_id")),
        "closed": sum(1 for x in callback_signals.values() if x.get("lifecycle_state") == "CLOSED"),
        "open_failed": sum(1 for x in callback_signals.values() if x.get("last_event") == "OPEN_FAILED"),
    }
    return {
        "policy_version": POLICY_VERSION,
        "cohort": COHORT,
        "active_paths": ACTIVE_PATHS,
        "executor_configured": bool(_executor_base_url() and _executor_secret()),
        "live_enable_requested": os.getenv("COMPANION_TRADEHOUSE_PILOT_ENABLED", "false").strip().lower() == "true",
        "summary": summary,
        "signals": signals,
        "callbacks": callback_signals,
    }


def _signal_id(market_key: str, path: str, setup_key: str, candidate: dict) -> str:
    raw = "|".join([
        COHORT, market_key, path, str(setup_key),
        str(candidate.get("direction") or ""), str(candidate.get("setup") or ""),
    ])
    digest = hashlib.sha256(raw.encode()).hexdigest()[:24]
    prefix = "OIL" if market_key == "USOIL" else "BTC"
    return f"{prefix}-{digest}"


def _fresh_enough(created_at: str | None, max_age_seconds: int = 120) -> bool:
    if not created_at:
        return False
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return 0 <= (datetime.now(timezone.utc) - dt).total_seconds() <= max_age_seconds
    except Exception:
        return False


async def deliver_selected_signal(
    market_key: str,
    profiles: dict,
    live_candidate: dict | None = None,
    setup_key: str | None = None,
    live_gate: str | None = None,
) -> dict:
    """Deliver exactly one approved fresh path per executable market.

    Live evaluation is independent of the paper-wallet open/closed state. A paper
    position may remain open for research while a genuinely new qualifying setup
    can still be delivered. Repeated scans of the same setup candle reuse one
    permanent signal_id and therefore cannot stack duplicate live entries.
    """
    if market_key not in ACTIVE_PATHS:
        return {"eligible": False, "sent": False, "status": "REFUSED_INSTRUMENT"}

    path = ACTIVE_PATHS[market_key]
    summary = profiles.get(path) or {}
    policy = summary.get("research_policy") or {}
    if policy.get("cohort") != COHORT:
        return {"eligible": False, "sent": False, "status": "WRONG_COHORT"}

    if not live_candidate:
        return {
            "eligible": True, "sent": False, "status": "NO_FRESH_QUALIFYING_SETUP",
            "path": path, "live_gate": live_gate or "NO_SIGNAL",
        }

    created_at = str(live_candidate.get("created_at") or "")
    if not _fresh_enough(created_at):
        return {"eligible": True, "sent": False, "status": "STALE_SIGNAL", "path": path, "live_gate": live_gate}

    stable_setup_key = str(setup_key or created_at)
    signal_id = _signal_id(market_key, path, stable_setup_key, live_candidate)
    state = _read(_state_path())
    signals = state.setdefault("signals", {})
    existing = signals.get(signal_id)
    if existing and existing.get("http_status") in (200, 202):
        return {
            "eligible": True, "sent": False, "status": "ALREADY_RECEIVED",
            "signal_id": signal_id, "ack": existing.get("ack"), "path": path,
            "setup_key": stable_setup_key,
        }

    base = _executor_base_url()
    secret = _executor_secret()
    if not (base and secret):
        return {"eligible": True, "sent": False, "status": "EXECUTOR_UNCONFIGURED", "signal_id": signal_id, "path": path}

    direction = str(live_candidate.get("direction") or "").upper()
    if direction not in {"LONG", "SHORT"}:
        return {"eligible": False, "sent": False, "status": "INVALID_DIRECTION", "signal_id": signal_id, "path": path}

    payload = {
        "signal_id": signal_id,
        "direction": direction,
        "instrument": EXECUTOR_INSTRUMENT[market_key],
        "cohort": COHORT,
        "path": path,
        "entry": live_candidate.get("reference_price"),
        "signal_created_at": created_at,
    }
    record = {
        "signal_id": signal_id, "market": market_key, "instrument": payload["instrument"],
        "path": path, "cohort": COHORT, "setup_key": stable_setup_key,
        "live_gate": live_gate, "payload": payload, "attempted_at": _utcnow(),
    }
    signals[signal_id] = record
    _write(_state_path(), state)

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.post(
                f"{base}/api/companion/ingest",
                headers={"x-executor-secret": secret, "Content-Type": "application/json"},
                json=payload,
            )
        try:
            ack: Any = response.json()
        except Exception:
            ack = {"raw": response.text[:1000]}
        record.update(http_status=response.status_code, ack=ack, updated_at=_utcnow())
        signals[signal_id] = record
        _write(_state_path(), state)
        return {
            "eligible": True, "sent": True, "signal_id": signal_id,
            "http_status": response.status_code,
            "accepted": bool(isinstance(ack, dict) and ack.get("accepted")),
            "opened": bool(isinstance(ack, dict) and ack.get("opened")),
            "lifecycle_state": ack.get("lifecycle_state") if isinstance(ack, dict) else None,
            "reason": ack.get("reason") if isinstance(ack, dict) else None,
            "reason_code": ack.get("reason_code") if isinstance(ack, dict) else None,
            "path": path, "setup_key": stable_setup_key,
        }
    except Exception as exc:
        record.update(status="DELIVERY_ERROR", error=f"{type(exc).__name__}: {exc}", updated_at=_utcnow())
        signals[signal_id] = record
        _write(_state_path(), state)
        return {"eligible": True, "sent": False, "status": "DELIVERY_ERROR", "signal_id": signal_id, "error": record["error"], "path": path}


def verify_callback_signature(body: bytes, headers: Any) -> bool:
    secret = (_first_env("COMPANION_CALLBACK_HMAC_SECRET", "TRADEHOUSE_CALLBACK_HMAC_SECRET") or _executor_secret())
    if not secret:
        return False
    supplied = (headers.get("x-tradehouse-signature") or headers.get("x-executor-signature") or headers.get("x-signature") or "").strip()
    if supplied.startswith("sha256="):
        supplied = supplied[7:]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(supplied) and hmac.compare_digest(supplied.lower(), expected.lower())


def record_callback(payload: dict) -> tuple[bool, str]:
    signal_id = str(payload.get("signal_id") or "").strip()
    event_id = str(payload.get("event_id") or "").strip()
    event_type = str(payload.get("event_type") or payload.get("event") or "").strip().upper()
    try:
        sequence = int(payload.get("event_sequence"))
    except Exception:
        return False, "INVALID_EVENT_SEQUENCE"
    if not signal_id:
        return False, "MISSING_SIGNAL_ID"
    if not event_id:
        return False, "MISSING_EVENT_ID"
    if event_type not in CALLBACK_EVENTS:
        return False, "INVALID_EVENT_TYPE"

    state = _read(_callback_path())
    signals = state.setdefault("signals", {})
    sig = signals.setdefault(signal_id, {"last_sequence": -1, "events": {}, "lifecycle_state": None})
    if event_id in sig["events"]:
        return True, "DUPLICATE_EVENT"
    if sequence <= int(sig.get("last_sequence", -1)):
        return False, "NON_MONOTONIC_EVENT_SEQUENCE"

    if event_type == "OPENED" and not payload.get("broker_position_id"):
        return False, "OPENED_MISSING_BROKER_POSITION_ID"

    sig["events"][event_id] = payload
    sig["last_sequence"] = sequence
    sig["last_event"] = event_type
    sig["updated_at"] = _utcnow()
    if event_type == "OPENED":
        sig["broker_position_id"] = payload.get("broker_position_id")
        sig["actual_fill_price"] = payload.get("actual_fill_price")
        sig["lifecycle_state"] = "OPENED"
    elif event_type == "CLOSED":
        sig["net_realized_pnl_usd"] = payload.get("net_realized_pnl_usd")
        sig["lifecycle_state"] = "CLOSED"
    else:
        sig["lifecycle_state"] = event_type
    _write(_callback_path(), state)
    return True, "RECORDED"
