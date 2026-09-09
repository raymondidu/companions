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
    return {
        "policy_version": POLICY_VERSION,
        "cohort": COHORT,
        "active_paths": ACTIVE_PATHS,
        "executor_configured": bool(os.getenv("COMPANION_EXECUTOR_BASE_URL", "").strip() and os.getenv("COMPANION_EXECUTOR_SECRET", "").strip()),
        "live_enable_requested": os.getenv("COMPANION_TRADEHOUSE_PILOT_ENABLED", "false").strip().lower() == "true",
        "signals": state.get("signals", {}),
        "callbacks": callbacks.get("signals", {}),
    }


def _signal_id(market_key: str, path: str, row: dict) -> str:
    raw = f"{COHORT}|{market_key}|{path}|{row.get('id')}|{row.get('opened_at') or row.get('created_at')}"
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


async def deliver_selected_signal(market_key: str, profiles: dict) -> dict:
    """Deliver exactly one approved path per executable market. Everything else remains research-only."""
    if market_key not in ACTIVE_PATHS:
        return {"eligible": False, "sent": False, "status": "REFUSED_INSTRUMENT"}

    path = ACTIVE_PATHS[market_key]
    summary = profiles.get(path) or {}
    policy = summary.get("research_policy") or {}
    if policy.get("cohort") != COHORT:
        return {"eligible": False, "sent": False, "status": "WRONG_COHORT"}

    recent = summary.get("recent") or []
    if not recent:
        return {"eligible": True, "sent": False, "status": "NO_SIGNAL"}
    row = recent[0]
    if row.get("status") != "OPEN":
        return {"eligible": True, "sent": False, "status": "NO_NEW_OPEN_SIGNAL"}

    created_at = row.get("opened_at") or row.get("created_at")
    signal_id = _signal_id(market_key, path, row)
    state = _read(_state_path())
    signals = state.setdefault("signals", {})
    existing = signals.get(signal_id)
    if existing and existing.get("http_status") in (200, 202):
        return {"eligible": True, "sent": False, "status": "ALREADY_RECEIVED", "signal_id": signal_id, "ack": existing.get("ack")}

    if not _fresh_enough(created_at):
        signals[signal_id] = {"signal_id": signal_id, "status": "STALE_SIGNAL_LOCAL_SKIP", "updated_at": _utcnow()}
        _write(_state_path(), state)
        return {"eligible": True, "sent": False, "status": "STALE_SIGNAL", "signal_id": signal_id}

    base = os.getenv("COMPANION_EXECUTOR_BASE_URL", "").strip().rstrip("/")
    secret = os.getenv("COMPANION_EXECUTOR_SECRET", "").strip()
    if not (base and secret):
        return {"eligible": True, "sent": False, "status": "EXECUTOR_UNCONFIGURED", "signal_id": signal_id}

    payload = {
        "signal_id": signal_id,
        "direction": row.get("direction"),
        "instrument": EXECUTOR_INSTRUMENT[market_key],
        "cohort": COHORT,
        "path": path,
        "entry": row.get("reference_price"),
        "signal_created_at": created_at,
    }
    record = {
        "signal_id": signal_id,
        "market": market_key,
        "instrument": payload["instrument"],
        "path": path,
        "cohort": COHORT,
        "payload": payload,
        "attempted_at": _utcnow(),
    }
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
            "eligible": True,
            "sent": True,
            "signal_id": signal_id,
            "http_status": response.status_code,
            "accepted": bool(isinstance(ack, dict) and ack.get("accepted")),
            "opened": bool(isinstance(ack, dict) and ack.get("opened")),
            "lifecycle_state": ack.get("lifecycle_state") if isinstance(ack, dict) else None,
            "reason": ack.get("reason") if isinstance(ack, dict) else None,
            "reason_code": ack.get("reason_code") if isinstance(ack, dict) else None,
        }
    except Exception as exc:
        record.update(status="DELIVERY_ERROR", error=f"{type(exc).__name__}: {exc}", updated_at=_utcnow())
        signals[signal_id] = record
        _write(_state_path(), state)
        return {"eligible": True, "sent": False, "status": "DELIVERY_ERROR", "signal_id": signal_id, "error": record["error"]}


def verify_callback_signature(body: bytes, headers: Any) -> bool:
    secret = (os.getenv("COMPANION_CALLBACK_HMAC_SECRET", "").strip() or os.getenv("COMPANION_EXECUTOR_SECRET", "").strip())
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

    sig["events"][event_id] = payload
    sig["last_sequence"] = sequence
    sig["last_event"] = event_type
    sig["updated_at"] = _utcnow()
    if event_type == "OPENED":
        if not payload.get("broker_position_id"):
            return False, "OPENED_MISSING_BROKER_POSITION_ID"
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
