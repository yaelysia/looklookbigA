import copy
import fcntl
import hashlib
import json
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import market_calendar


MARKET = "CN_A"
SCHEMA_VERSION = 1
LEASE_SECONDS = 300
RETRY_DELAY_SECONDS = 60
CACHE_MAX_AGE_SECONDS = 600


def _history_root():
    return Path(os.environ.get("MARKET_HISTORY_DIR", ".market-data/history"))


def _iso(value):
    return value.astimezone(market_calendar.CST).isoformat(timespec="seconds")


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=market_calendar.CST)
    return parsed.astimezone(market_calendar.CST)


def _aware_cst(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=market_calendar.CST)
    return value.astimezone(market_calendar.CST)


def _segment(now, calendar):
    afternoon_bootstrap_at = calendar["acquisition_windows_parsed"][1][0]
    current = now.time().replace(tzinfo=None)
    return "AFTERNOON" if current >= afternoon_bootstrap_at else "MORNING"


def session_identity(now):
    now = _aware_cst(now)
    calendar = market_calendar.load_calendar()
    verification = market_calendar.trading_day_verification(now.date(), calendar)
    base = {
        "market": MARKET,
        "session_date": now.date().isoformat(),
        "session_segment": None,
        "required": False,
        "state": "NOT_REQUIRED",
        "reason": None,
    }
    if verification.get("verification_status") != "VERIFIED":
        return {**base, "state": "UNVERIFIED", "reason": verification.get("reason")}
    if verification.get("is_trading_day") is not True:
        return {**base, "reason": verification.get("reason")}
    if not market_calendar.in_market_window(now, calendar):
        return {**base, "reason": "OUTSIDE_ACQUISITION_WINDOW"}
    segment = _segment(now, calendar)
    return {
        **base,
        "session_segment": segment,
        "required": True,
        "state": "PENDING",
        "reason": None,
        "bootstrap_key": f"{MARKET}:{now.date().isoformat()}:{segment}",
    }


def _state_path(identity):
    return (
        _history_root()
        / "breadth_bootstrap"
        / identity["market"]
        / identity["session_date"]
        / f"{identity['session_segment']}.json"
    )


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "bootstrap_state": "UNVERIFIED",
            "error": f"invalid bootstrap state: {type(exc).__name__}: {exc}",
        }
    return value if isinstance(value, dict) else None


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        temporary = Path(handle.name)
    os.replace(temporary, path)


@contextmanager
def _state_lock(path):
    lock_name = hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest() + ".lock"
    lock_path = Path(tempfile.gettempdir()) / "looklookbiga-breadth-locks" / lock_name
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _revision():
    def number(name):
        try:
            return int(os.environ.get(name) or 0) or None
        except ValueError:
            return None

    return {
        "run_id": number("GITHUB_RUN_ID"),
        "run_attempt": number("GITHUB_RUN_ATTEMPT"),
        "head_sha": (os.environ.get("GITHUB_SHA") or "").lower() or None,
        "process_id": os.getpid(),
        "thread_id": threading.get_ident(),
    }


def _base_response(identity, now, state, reason=None):
    return {
        "status": "ERROR" if state not in {"NOT_REQUIRED"} else "NOT_REQUIRED",
        "source": "FAST session breadth bootstrap",
        "collected_at_cst": now.strftime("%Y-%m-%d %H:%M:%S"),
        "market_session_date": identity.get("session_date"),
        "session_date": identity.get("session_date"),
        "session_segment": identity.get("session_segment"),
        "freshness": "UNKNOWN",
        "freshness_basis": "SESSION_SEGMENT_BOOTSTRAP_STATE",
        "bootstrap_state": state,
        "bootstrap_key": identity.get("bootstrap_key"),
        "bootstrap_at": None,
        "cache_age_seconds": None,
        "source_session": None,
        "fetched_at": None,
        "age_seconds": None,
        "freshness_status": "UNKNOWN",
        "bootstrap_revision": None,
        "quality": "DEGRADED" if state in {"PENDING", "NOT_REQUIRED"} else "FAILED",
        "error": reason,
        "provenance": {
            "type": "CACHE_CONTROL",
            "market": identity.get("market"),
            "session_binding": identity.get("bootstrap_key"),
            "state_store": "market-history artifact",
        },
    }


def _ready_response(identity, state, now, access):
    breadth = copy.deepcopy(state.get("breadth") or {})
    bootstrap_at = _parse_time(state.get("bootstrap_at"))
    age = max(0, int((now - bootstrap_at).total_seconds())) if bootstrap_at else None
    stale = age is None or age > CACHE_MAX_AGE_SECONDS
    original_status = breadth.get("status")
    if stale:
        breadth["status"] = "PARTIAL" if original_status in {"OK", "PARTIAL"} else "ERROR"
        breadth["freshness"] = "STALE"
        breadth["quality"] = "DEGRADED"
        bootstrap_state = "STALE"
    else:
        breadth["quality"] = "PASS" if original_status == "OK" else "DEGRADED"
        bootstrap_state = "READY"
    breadth.update(
        {
            "source": f"Fast session bootstrap cache <- {breadth.get('source') or 'market breadth'}",
            "market_session_date": identity["session_date"],
            "session_date": identity["session_date"],
            "session_segment": identity["session_segment"],
            "freshness_basis": "SESSION_SEGMENT_BOOTSTRAP_CACHE_AGE",
            "bootstrap_state": bootstrap_state,
            "bootstrap_key": identity["bootstrap_key"],
            "bootstrap_at": state.get("bootstrap_at"),
            "cache_age_seconds": age,
            "source_session": breadth.get("market_session_date"),
            "fetched_at": breadth.get("collected_at_cst") or state.get("bootstrap_at"),
            "age_seconds": age,
            "freshness_status": breadth.get("freshness") or "UNKNOWN",
            "bootstrap_revision": state.get("bootstrap_revision"),
            "fast_path": {
                "mode": "INTRADAY_FAST",
                "source": "SESSION_SEGMENT_BOOTSTRAP",
                "access": access,
                "age_seconds": age,
                "max_age_seconds": CACHE_MAX_AGE_SECONDS,
                "network_refresh": "BOOTSTRAP_OWNER" if access == "OWNER_FETCH" else "NOT_REQUIRED",
            },
            "provenance": {
                "type": "CACHE_CONTROL",
                "market": identity["market"],
                "session_binding": identity["bootstrap_key"],
                "state_store": "market-history artifact",
                "upstream_source": state.get("upstream_source"),
            },
        }
    )
    return breadth


def _validate_breadth(value, identity):
    if not isinstance(value, dict):
        raise ValueError("breadth source returned a non-object")
    if value.get("status") not in {"OK", "PARTIAL"}:
        raise ValueError(f"breadth source status is not reusable: {value.get('status')}")
    if value.get("market_session_date") != identity["session_date"]:
        raise ValueError(
            "breadth session mismatch: "
            f"expected={identity['session_date']} actual={value.get('market_session_date')}"
        )


def fetch_market_breadth(base, now, indices, source_fetch):
    now = _aware_cst(now)
    identity = session_identity(now)
    if not identity.get("required"):
        return _base_response(identity, now, identity["state"], identity.get("reason"))

    path = _state_path(identity)
    owner = f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
    with _state_lock(path):
        state = _read_json(path) or {}
        if state.get("bootstrap_key") not in {None, identity["bootstrap_key"]}:
            return _base_response(identity, now, "UNVERIFIED", "BOOTSTRAP_KEY_MISMATCH")
        if state.get("bootstrap_state") == "READY" and state.get("breadth"):
            return _ready_response(identity, state, now, "CACHE_HIT")

        lease_expires = _parse_time(state.get("lease_expires_at"))
        if state.get("bootstrap_state") == "PENDING" and lease_expires and now < lease_expires:
            pending = _base_response(identity, now, "PENDING", "BOOTSTRAP_OWNER_ACTIVE")
            pending["bootstrap_revision"] = state.get("bootstrap_revision")
            return pending

        next_retry = _parse_time(state.get("next_retry_at"))
        if state.get("bootstrap_state") == "FAILED" and next_retry and now < next_retry:
            failed = _base_response(identity, now, "FAILED", state.get("error") or "BOOTSTRAP_FAILED")
            failed["bootstrap_revision"] = state.get("bootstrap_revision")
            return failed

        attempts = int(state.get("attempts") or 0) + 1
        claimed = {
            "schema_version": SCHEMA_VERSION,
            **identity,
            "bootstrap_state": "PENDING",
            "owner": owner,
            "attempts": attempts,
            "claimed_at": _iso(now),
            "lease_expires_at": _iso(now + timedelta(seconds=LEASE_SECONDS)),
            "bootstrap_at": None,
            "next_retry_at": None,
            "bootstrap_revision": _revision(),
            "breadth": None,
            "error": None,
        }
        _write_json(path, claimed)

    try:
        breadth = source_fetch(base, now, indices)
        _validate_breadth(breadth, identity)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        with _state_lock(path):
            current = _read_json(path) or {}
            if current.get("bootstrap_state") == "PENDING" and current.get("owner") == owner:
                current.update(
                    {
                        "bootstrap_state": "FAILED",
                        "owner": None,
                        "failed_at": _iso(now),
                        "lease_expires_at": None,
                        "next_retry_at": _iso(now + timedelta(seconds=RETRY_DELAY_SECONDS)),
                        "error": error,
                    }
                )
                _write_json(path, current)
        failed = _base_response(identity, now, "FAILED", error)
        failed["bootstrap_revision"] = claimed["bootstrap_revision"]
        return failed

    with _state_lock(path):
        current = _read_json(path) or {}
        if current.get("bootstrap_state") != "PENDING" or current.get("owner") != owner:
            if current.get("bootstrap_state") == "READY" and current.get("breadth"):
                return _ready_response(identity, current, now, "CACHE_HIT_AFTER_RACE")
            return _base_response(identity, now, "UNVERIFIED", "BOOTSTRAP_OWNERSHIP_LOST")
        ready = {
            **current,
            "bootstrap_state": "READY",
            "owner": None,
            "bootstrap_at": _iso(now),
            "lease_expires_at": None,
            "next_retry_at": None,
            "upstream_source": breadth.get("source"),
            "breadth": breadth,
            "error": None,
        }
        _write_json(path, ready)
    return _ready_response(identity, ready, now, "OWNER_FETCH")
