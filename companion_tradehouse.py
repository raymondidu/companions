from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
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
    "ACCEPTED", "RECEIVED", "OPENING", "OPENED", "POSITION_UPDATE", "PROTECTION_ARMED",
    "PROTECTION_RAISED", "CLOSE_REQUESTED", "CLOSED", "OPEN_FAILED",
    "POSITION_LOST", "RECOVERED_AFTER_RESTART",
}
CALLBACK_MAX_AGE_MS = 5 * 60 * 1000


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_env(*names: str) -> str:
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


def _callback_secret() -> str:
    return _first_env(
        "COMPANION_CALLBACK_SECRET",
        "COMPANION_CALLBACK_HMAC_SECRET",
        "TRADEHOUSE_CALLBACK_HMAC_SECRET",
    ) or _executor_secret()


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


def _callback_position_rows(callbacks: dict):
    for signal_id, signal in (callbacks.get("signals", {}) or {}).items():
        positions = signal.get("positions", {}) if isinstance(signal, dict) else {}
        if positions:
            for tradehouse_id, position in positions.items():
                if isinstance(position, dict):
                    yield signal_id, tradehouse_id, position
        elif isinstance(signal, dict) and signal.get("broker_position_id"):
            yield signal_id, str(signal.get("tradehouse_id") or signal_id), signal


def delivery_snapshot() -> dict:
    state = _read(_state_path())
    callbacks = _read(_callback_path())
    signals = state.get("signals", {})
    callback_signals = callbacks.get("signals", {})
    rows = list(_callback_position_rows(callbacks))
    failed_rows = [(sid, tid, pos) for sid, tid, pos in rows if pos.get("last_event") == "OPEN_FAILED"]
    failed_signal_ids = {sid for sid, _, _ in failed_rows}
    failure_reasons: dict[str, int] = {}
    for _, _, pos in failed_rows:
        reason = str(
            pos.get("reason_code")
            or pos.get("error_code")
            or pos.get("open_failure_reason")
            or pos.get("broker_error_code")
            or pos.get("reason")
            or pos.get("error_message")
            or pos.get("broker_error_message")
            or pos.get("message")
            or "UNSPECIFIED_OPEN_FAILURE"
        )
        failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
    summary = {
        "generated": len(signals),
        "sent": sum(1 for x in signals.values() if x.get("attempted_at")),
        "accepted": sum(1 for x in signals.values() if x.get("http_status") in (200, 202) and isinstance(x.get("ack"), dict) and (x["ack"].get("accepted") is True or x["ack"].get("status") == "QUEUED")),
        "opened": sum(1 for _, _, x in rows if x.get("broker_position_id")),
        "closed": sum(1 for _, _, x in rows if x.get("lifecycle_state") == "CLOSED"),
        "open_failed": len(failed_rows),
        "open_failed_positions": len(failed_rows),
        "open_failed_signals": len(failed_signal_ids),
    }
    return {
        "policy_version": POLICY_VERSION,
        "cohort": COHORT,
        "active_paths": ACTIVE_PATHS,
        "executor_configured": bool(_executor_base_url() and _executor_secret()),
        "callback_configured": bool(_callback_secret()),
        "live_enable_requested": os.getenv("COMPANION_TRADEHOUSE_PILOT_ENABLED", "false").strip().lower() == "true",
        "summary": summary,
        "open_failure_reasons": dict(sorted(failure_reasons.items(), key=lambda kv: kv[1], reverse=True)),
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

    internal_direction = str(live_candidate.get("direction") or "").upper()
    if internal_direction not in {"LONG", "SHORT"}:
        return {"eligible": False, "sent": False, "status": "INVALID_DIRECTION", "signal_id": signal_id, "path": path}
    executor_direction = {"LONG": "BUY", "SHORT": "SELL"}[internal_direction]

    payload = {
        "signal_id": signal_id,
        "direction": executor_direction,
        "instrument": EXECUTOR_INSTRUMENT[market_key],
        "cohort": COHORT,
        "path": path,
        "entry": live_candidate.get("reference_price"),
        "signal_created_at": created_at,
        "signal": f"{EXECUTOR_INSTRUMENT[market_key]} {executor_direction.lower()} — {path}",
    }
    record = {
        "signal_id": signal_id, "market": market_key, "instrument": payload["instrument"],
        "path": path, "cohort": COHORT, "setup_key": stable_setup_key,
        "live_gate": live_gate, "internal_direction": internal_direction,
        "payload": payload, "attempted_at": _utcnow(),
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
        accepted = bool(
            isinstance(ack, dict)
            and response.status_code in (200, 202)
            and (ack.get("accepted") is True or ack.get("status") == "QUEUED" or ack.get("duplicate") is True or ack.get("validate_only") is True)
        )
        return {
            "eligible": True, "sent": True, "signal_id": signal_id,
            "http_status": response.status_code,
            "accepted": accepted,
            "opened": False,
            "lifecycle_state": ack.get("status") if isinstance(ack, dict) else None,
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
    secret = _callback_secret()
    if not secret:
        return False
    timestamp = str(headers.get("x-executor-timestamp") or "").strip()
    supplied = str(headers.get("x-executor-signature") or "").strip().lower()
    if not timestamp or not supplied:
        return False
    try:
        ts_ms = int(timestamp)
    except Exception:
        return False
    if abs(int(time.time() * 1000) - ts_ms) > CALLBACK_MAX_AGE_MS:
        return False
    try:
        signed = timestamp.encode("utf-8") + b"." + body
        expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest().lower()
    except Exception:
        return False
    return hmac.compare_digest(supplied, expected)


def record_callback(payload: dict) -> tuple[bool, str]:
    signal_id = str(payload.get("signal_id") or "").strip()
    event_id = str(payload.get("event_id") or "").strip()
    tradehouse_id = str(payload.get("tradehouse_id") or "").strip()
    event_type = str(payload.get("event_type") or payload.get("event") or "").strip().upper()
    pilot_kind = str(payload.get("pilot_kind") or "").strip()
    try:
        sequence = int(payload.get("event_sequence"))
    except Exception:
        return False, "INVALID_EVENT_SEQUENCE"
    if not signal_id:
        return False, "MISSING_SIGNAL_ID"
    if not event_id:
        return False, "MISSING_EVENT_ID"
    if not tradehouse_id:
        return False, "MISSING_TRADEHOUSE_ID"
    if event_type not in CALLBACK_EVENTS:
        return False, "INVALID_EVENT_TYPE"
    if pilot_kind and pilot_kind != "COMPANION_OILBTC":
        return False, "WRONG_PILOT_KIND"
    if event_type == "OPENED" and not payload.get("broker_position_id"):
        return False, "OPENED_MISSING_BROKER_POSITION_ID"

    state = _read(_callback_path())
    seen = state.setdefault("event_ids", {})
    if event_id in seen:
        return True, "DUPLICATE_EVENT"

    signals = state.setdefault("signals", {})
    sig = signals.setdefault(signal_id, {"positions": {}, "updated_at": _utcnow()})
    positions = sig.setdefault("positions", {})
    pos = positions.setdefault(tradehouse_id, {
        "tradehouse_id": tradehouse_id,
        "signal_id": signal_id,
        "last_sequence": -1,
        "events": {},
        "lifecycle_state": None,
    })
    if sequence <= int(pos.get("last_sequence", -1)):
        return False, "NON_MONOTONIC_EVENT_SEQUENCE"

    pos["events"][event_id] = payload
    pos["last_sequence"] = sequence
    pos["last_event"] = event_type
    pos["updated_at"] = _utcnow()
    for field in (
        "broker_position_id", "instrument", "direction", "symbol", "actual_fill_price",
        "filled_lot", "strategy_capital_usd", "net_realized_pnl_usd",
        "net_realized_return_pct", "close_reason", "pilot_kind",
        "reason", "reason_code", "error", "error_code", "error_message", "message",
        "broker_error", "broker_error_code", "broker_error_message", "open_failure_reason",
    ):
        if payload.get(field) is not None:
            pos[field] = payload.get(field)
    pos["lifecycle_state"] = str(payload.get("lifecycle_state") or event_type).upper()
    if event_type == "OPENED":
        pos["lifecycle_state"] = "OPENED"
    elif event_type == "CLOSED":
        pos["lifecycle_state"] = "CLOSED"

    seen[event_id] = {"signal_id": signal_id, "tradehouse_id": tradehouse_id, "recorded_at": _utcnow()}
    sig["last_event"] = event_type
    sig["last_tradehouse_id"] = tradehouse_id
    sig["last_sequence"] = sequence
    sig["lifecycle_state"] = pos["lifecycle_state"]
    sig["updated_at"] = _utcnow()
    for field in (
        "broker_position_id", "actual_fill_price", "net_realized_pnl_usd",
        "instrument", "direction", "symbol", "close_reason", "reason", "reason_code",
        "error", "error_code", "error_message", "message", "broker_error",
        "broker_error_code", "broker_error_message", "open_failure_reason",
    ):
        if pos.get(field) is not None:
            sig[field] = pos.get(field)

    positions[tradehouse_id] = pos
    signals[signal_id] = sig
    _write(_callback_path(), state)
    return True, "RECORDED"
