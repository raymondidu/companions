from __future__ import annotations

import asyncio
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
# OWNER PAUSE, 2026-09-16: "Pause all scanning."
#
# Companions is NOT paper-only, whatever its status payload says. scan_one emits
# 'tradehouse_delivery': False and 'live_authority': False as literal labels in
# the output dict, while companion_runner.py:149 calls deliver_selected_signal
# on EVERY scan and this module posts to the executor with x-executor-secret.
# A field that reports a delivery path as off while the code below delivers is
# the instrument lying about the thing it exists to report.
#
# So the pause is enforced HERE, at the post, and not only at the scan: it is
# the last thing between a signal and a real order, and stopping it is what
# stops the trades and the mail they generate. Blocking only the scan would
# leave this reachable by any other caller.
#
# Owner instruction, 2026-09-16, verbatim: "let all go live now and start sending
# trades", reaffirmed as "Turn it on do what I say". The pause is lifted.
#
# Every fail-closed check above the post is UNCHANGED and still runs first:
# INVALID_DIRECTION, WRONG_COHORT, STALE_SIGNAL, EXECUTOR_UNCONFIGURED and
# REFUSED_INSTRUMENT each answer for themselves before anything is sent. Lifting
# this flag removes the owner's stop and nothing else.
#
# One line to stop again: set this True and redeploy.
COMPANION_SCANNING_PAUSED = False

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


def delivery_snapshot(ledger: str = 'full') -> dict:
    """The execution-truth snapshot. `ledger` decides how much of it travels.

    THREE MODES, because the three callers need genuinely different amounts:
      'full'   -- everything. /api/companion/tradehouse, for detail reads.
      'none'   -- omit signals and callbacks entirely. /health, whose consumers
                  were each checked and read neither.
      'latest' -- only the newest signal per market and that signal's callback.
                  /api/markets, because the dashboard only ever uses those: its
                  latestSignal() filters by market, sorts by attempted_at desc
                  and takes [0], and lifecycle() looks up exactly that one
                  signal_id. Sending only the newest per market is therefore
                  BEHAVIOURALLY IDENTICAL to the page, with no JS change.

    MEASURED 2026-09-16: `signals` and `callbacks` carry the whole delivery and
    callback ledgers inline, and /health returned a 28,348,272-byte body because
    of them. curl --max-time 5, which is what the deploy verifier uses, got
    23,884,760 of those bytes and gave up, so every companion deploy failed its
    own health check and companion-telemetry failed 11 of 13 runs. Nothing was
    wrong with the box; the payload was simply unfetchable.

    NOTHING THAT READS /health NEEDS THEM. Checked before trimming, not assumed:
    the deploy verifier reads executor_configured and live_enable_requested, the
    callback auth probe reads callback_rejections, and
    .github/scripts/companion_health_assert.py reads summary, lifecycle,
    open_failure_reasons, markets and live_signal_gate -- zero references to
    `callbacks`, and every `signals` hit is opened_signals or closed_signals
    INSIDE summary. The dashboard does need both, and it reads /api/markets,
    which is left untouched.

    THE COUNTS REPLACE THEM RATHER THAN THE KEYS JUST VANISHING. A field that
    silently disappears reads as zero to whatever consumed it next, which is the
    fault this file has been chasing all day.
    """
    state = _read(_state_path())
    # WHY CALLBACKS ARE BEING TURNED AWAY, not just how many. Without this the
    # only visible symptom is 401s in an access log, which cannot separate a
    # secret mismatch from a stale replay.
    callback_rejects = callback_rejection_counts()
    callbacks = _read(_callback_path())
    signals = state.get("signals", {})
    callback_signals = callbacks.get("signals", {})
    rows = list(_callback_position_rows(callbacks))
    failed_rows = [(sid, tid, pos) for sid, tid, pos in rows if pos.get("last_event") == "OPEN_FAILED"]
    failed_signal_ids = {sid for sid, _, _ in failed_rows}
    failure_reasons: dict[str, int] = {}
    # When an OPEN_FAILED callback carries none of the eight reason fields below,
    # the failure lands in UNSPECIFIED_OPEN_FAILURE and nobody can say why the
    # position did not open. 129 of them had accumulated by 2026-09-10 with no
    # way to tell whether the executor sends no reason at all, or sends one under
    # a key this list does not read. Recording the KEY NAMES present on those
    # payloads answers that without guessing. Names only, never values: these
    # rows are executor data and may carry account or broker detail.
    unspecified_keys: set[str] = set()
    unspecified_event_keys: set[str] = set()
    unspecified_event_counts: list[int] = []
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
        if reason == "UNSPECIFIED_OPEN_FAILURE" and isinstance(pos, dict):
            unspecified_keys.update(str(k) for k in pos)
            # The top level carries only identifiers and lifecycle state, so if a
            # reason exists at all it is inside the per-event history. Record the
            # field names on the OPEN_FAILED event itself: that is the difference
            # between a one-line fix here and an ask to the executor.
            # Every event, not just ones matching a guessed event-name key: the
            # first pass filtered on ev['event'] / ev['type'] and returned
            # nothing, which cannot distinguish "no events" from "my guess at
            # the key was wrong". Counting them separates those two.
            evs = pos.get("events") or []
            unspecified_event_counts.append(len(evs) if isinstance(evs, list) else -1)
            for ev in (evs if isinstance(evs, list) else []):
                if isinstance(ev, dict):
                    unspecified_event_keys.update(str(k) for k in ev)
        failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
    # generated/sent/accepted count SIGNALS. opened/closed count POSITIONS, because
    # one signal fans out to many funded accounts. Mixing the two in one row is why
    # opened could read higher than accepted and look impossible. Both bases are now
    # published explicitly; the original keys keep their existing meaning so nothing
    # reading them changes.
    opened_rows = [(sid, tid, x) for sid, tid, x in rows if x.get("broker_position_id")]
    closed_rows = [(sid, tid, x) for sid, tid, x in rows if x.get("lifecycle_state") == "CLOSED"]
    summary = {
        "generated": len(signals),
        "sent": sum(1 for x in signals.values() if x.get("attempted_at")),
        "accepted": sum(1 for x in signals.values() if x.get("http_status") in (200, 202) and isinstance(x.get("ack"), dict) and (x["ack"].get("accepted") is True or x["ack"].get("status") == "QUEUED")),
        "opened": len(opened_rows),
        "closed": len(closed_rows),
        # Explicit per-basis counts. TradeHouse's "opened for N accounts" is
        # opened_positions; distinct signals that opened anywhere is opened_signals.
        "opened_positions": len(opened_rows),
        "closed_positions": len(closed_rows),
        "opened_signals": len({sid for sid, _, _ in opened_rows}),
        "closed_signals": len({sid for sid, _, _ in closed_rows}),
        "counting_basis": {
            "signals": ["generated", "sent", "accepted", "opened_signals", "closed_signals", "open_failed_signals"],
            "positions": ["opened_positions", "closed_positions", "open_failed_positions"],
            "note": "one signal fans out to many accounts; position counts can exceed signal counts",
        },
        "open_failed": len(failed_rows),
        "open_failed_positions": len(failed_rows),
        "open_failed_signals": len(failed_signal_ids),
    }
    out = {
        "policy_version": POLICY_VERSION,
        "cohort": COHORT,
        "active_paths": ACTIVE_PATHS,
        "executor_configured": bool(_executor_base_url() and _executor_secret()),
        "callback_configured": bool(_callback_secret()),
        # WHY callbacks were turned away, since this process started. A bare
        # count of 401s cannot separate a secret mismatch from a stale replay,
        # and those need opposite fixes.
        "callback_rejections": callback_rejects,
        "callback_max_age_ms": CALLBACK_MAX_AGE_MS,
        "live_enable_requested": os.getenv("COMPANION_TRADEHOUSE_PILOT_ENABLED", "false").strip().lower() == "true",
        "summary": summary,
        "open_failure_reasons": dict(sorted(failure_reasons.items(), key=lambda kv: kv[1], reverse=True)),
        # Field names seen on failures that carried no readable reason.
        "unspecified_open_failure_keys": sorted(unspecified_keys),
        "unspecified_open_failure_event_keys": sorted(unspecified_event_keys),
        "unspecified_open_failure_event_count_max": max(unspecified_event_counts, default=0),
        "unspecified_open_failure_rows_with_events": sum(1 for n in unspecified_event_counts if n > 0),
        "signals": signals,
        "callbacks": callback_signals,
    }
    if ledger == "none":
        out.pop("signals", None)
        out.pop("callbacks", None)
        out["ledger_omitted"] = {
            "reason": "these two keys are the whole ledger and made /health a 28MB body",
            "mode": "none",
            "signals_count": len(signals or {}),
            "callback_signals_count": len(callback_signals or {}),
            "full_payload_at": "/api/companion/tradehouse",
        }
    elif ledger == "latest":
        # Newest per market, by the same key the page sorts on.
        newest: dict[str, tuple[str, str]] = {}
        for sid, sig in (signals or {}).items():
            if not isinstance(sig, dict):
                continue
            market = str(sig.get("market") or "")
            stamp = str(sig.get("attempted_at") or sig.get("updated_at") or "")
            current = newest.get(market)
            if current is None or stamp > current[1]:
                newest[market] = (sid, stamp)
        keep = {sid for sid, _ in newest.values()}
        out["signals"] = {k: v for k, v in (signals or {}).items() if k in keep}
        out["callbacks"] = {k: v for k, v in (callback_signals or {}).items()
                            if k in keep}
        out["ledger_omitted"] = {
            "reason": "the dashboard only reads the newest signal per market and "
                      "its callback; the full ledger made this a 28MB body and the "
                      "page could not finish loading",
            "mode": "latest",
            "signals_count": len(signals or {}),
            "signals_kept": len(out["signals"]),
            "callback_signals_count": len(callback_signals or {}),
            "full_payload_at": "/api/companion/tradehouse",
        }
    return out


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

    if COMPANION_SCANNING_PAUSED:
        # Owner pause, sitting DIRECTLY on the post and deliberately not at the
        # top of this function.
        #
        # First placement was the top, and it broke ten routing-contract tests
        # by answering PAUSED_BY_OWNER where they require INVALID_DIRECTION,
        # WRONG_COHORT, STALE_SIGNAL, EXECUTOR_UNCONFIGURED or
        # REFUSED_INSTRUMENT. Those are fail-closed trading gates and making
        # them unreachable is softening them, which is never allowed -- they
        # still have to hold the day the pause is lifted.
        #
        # Here, every one of those refusals runs first and returns its own
        # status, and the pause catches only what would otherwise have been
        # SENT. Nothing can reach the post: this is the last statement before
        # the payload is built.
        return {
            "eligible": True, "sent": False, "status": "PAUSED_BY_OWNER",
            "signal_id": signal_id, "path": path,
        }

    # TradeHouse Companion ingest contract requires LONG/SHORT exactly.
    # Keep internal strategy semantics unchanged and send them through verbatim.
    executor_direction = internal_direction

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


# Why a callback was turned away, counted in memory and surfaced in telemetry.
# The HTTP response stays a bare 401 UNAUTHORIZED_CALLBACK: telling a caller
# WHICH check it failed is an oracle, and this endpoint is unauthenticated until
# the signature passes.
_CALLBACK_REJECTS: dict[str, int] = {}


def callback_rejection_reason(body: bytes, headers: Any) -> str:
    """The reason a callback fails verification, or '' when it passes.

    ONE 401 WAS ANSWERING FOUR DIFFERENT QUESTIONS. On 2026-09-16 the companion
    dashboard was serving a continuous callback flood with roughly 40% rejected,
    and that was reported as "40% failing signature verification". It was not
    knowable: an unset secret, missing headers, a STALE TIMESTAMP and a genuinely
    wrong signature all returned the same bare False. A catch-all reject reason
    cannot name a cause, and this one was used to describe the executor's
    behaviour for hours.

    It matters because TradeHouse was replaying a backlog of 43,350 undelivered
    events retrying up to 20 times each. CALLBACK_MAX_AGE_MS is FIVE MINUTES, so
    events older than that can NEVER verify however correct the signature is.
    STALE_TIMESTAMP and BAD_SIGNATURE need different fixes -- drop the backlog
    versus align the secret -- and they were indistinguishable.

    The verification itself is UNCHANGED: same checks, same order, same
    constant-time compare. This only names the branch that was already taken.
    """
    secret = _callback_secret()
    if not secret:
        return "NO_CALLBACK_SECRET_CONFIGURED"
    timestamp = str(headers.get("x-executor-timestamp") or "").strip()
    supplied = str(headers.get("x-executor-signature") or "").strip().lower()
    if not timestamp:
        return "MISSING_TIMESTAMP_HEADER"
    if not supplied:
        return "MISSING_SIGNATURE_HEADER"
    try:
        ts_ms = int(timestamp)
    except Exception:
        return "TIMESTAMP_NOT_AN_INTEGER"
    age_ms = abs(int(time.time() * 1000) - ts_ms)
    if age_ms > CALLBACK_MAX_AGE_MS:
        # Seconds instead of milliseconds lands here too, and looks identical to
        # a genuinely old event. Both are the caller's to fix and neither is a
        # signature problem.
        return "STALE_TIMESTAMP"
    try:
        signed = timestamp.encode("utf-8") + b"." + body
        expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest().lower()
    except Exception:
        return "SIGNATURE_COMPUTATION_FAILED"
    if not hmac.compare_digest(supplied, expected):
        return "BAD_SIGNATURE"
    return ""


def verify_callback_signature(body: bytes, headers: Any) -> bool:
    """FAIL-CLOSED. Unchanged behaviour; the reason is recorded, not returned."""
    reason = callback_rejection_reason(body, headers)
    if reason:
        _CALLBACK_REJECTS[reason] = _CALLBACK_REJECTS.get(reason, 0) + 1
        return False
    _CALLBACK_REJECTS["ACCEPTED"] = _CALLBACK_REJECTS.get("ACCEPTED", 0) + 1
    return True


def callback_rejection_counts() -> dict[str, int]:
    """Counts since this process started. Names only, never a header value."""
    return dict(sorted(_CALLBACK_REJECTS.items(), key=lambda kv: -kv[1]))


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


# THE CALLBACK LEDGER IS 34.5 MB AND record_callback REWRITES ALL OF IT.
# Measured on the box 2026-09-16 18:48: tradehouse_callbacks.json is
# 34,496,030 bytes across 71 signals and 956 position rows. record_callback
# does a full _read at the top and a full _write at the bottom, both
# synchronous, and companion_app awaited it directly inside `async def
# tradehouse_callback`. So EVERY inbound callback parsed, re-serialised and
# rewrote 34.5 MB ON THE EVENT LOOP, which stalls the whole process: no other
# request can be accepted or answered while it runs. That is why /health has
# been unanswerable for days, why a container recreate never helped (the file
# lives on the volume and survives), and why the dashboard sat at 90-97% CPU.
#
# I FIRST BLAMED delivery_snapshot() AND MEASURED IT INSTEAD OF SHIPPING THE
# GUESS: 585 ms, nowhere near the 25 s timeouts, and /health is a plain `def`
# so FastAPI already runs it in a threadpool. The probe said
# AGGREGATION_IS_NOT_THE_COST and it was right. This is the cost.
#
# THE LOCK IS NOT OPTIONAL. Running record_callback in a thread without one
# would be worse than the bug: the event loop was serialising these
# read-modify-write cycles for free, and threads would let two callbacks read
# the same state and the second overwrite the first, silently losing a real
# trade event. The lock keeps the exact ordering guarantee the loop gave while
# freeing the loop itself.
#
# Nothing about verification, admission or recording changes. This moves WHERE
# record_callback runs, not WHAT it does; verify_callback_signature still runs
# fail-closed before it, untouched.
_CALLBACK_RECORD_LOCK = asyncio.Lock()


async def record_callback_async(payload: dict) -> tuple[bool, str]:
    """record_callback, off the event loop and still strictly serialised."""
    async with _CALLBACK_RECORD_LOCK:
        return await asyncio.to_thread(record_callback, payload)
