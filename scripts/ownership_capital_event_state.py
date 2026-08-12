import json
from datetime import date, datetime
from pathlib import Path

import company_events
import ownership_capital_base as core

TARGET_PLAN_TYPES = ("BUYBACK", "HOLDER_INCREASE", "HOLDER_DECREASE")
TARGET_TYPES = TARGET_PLAN_TYPES + ("UNLOCK",)
PLAN_HISTORY_LIMIT = 40
UNLOCK_HISTORY_LIMIT = 40
COVERAGE_TARGET_DAYS = 90
WINDOWS = (7, 30, 90, 180)


def _first(mapping, *names):
    if not isinstance(mapping, dict):
        return None
    for name in names:
        value = mapping.get(name)
        if value not in (None, "", "-"):
            return value
    return None


def _date(value):
    text = core._date(value)
    if not text:
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _number(mapping, *names):
    return core._as_float(_first(mapping, *names))


def _event_published_date(event):
    return _date((event or {}).get("published_at"))


def _as_of_date(snapshot):
    for key in ("runner_time_cst", "runner_time_utc"):
        value = snapshot.get(key)
        text = _date(value)
        if text:
            return text
    return datetime.now(core.CST).date().isoformat()


def _event_cache(code):
    try:
        path = company_events._event_cache_path(code)
        payload = company_events._read_json(path)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("code") or code).zfill(6) != str(code).zfill(6):
        return None
    if payload.get("source") not in (None, "CNINFO"):
        return None
    if not isinstance(payload.get("events"), list):
        return None
    return payload


def _events_for_stock(code, stock):
    by_id = {}
    cache = _event_cache(code)
    for event in (cache or {}).get("events") or []:
        if isinstance(event, dict) and event.get("event_id"):
            by_id[event["event_id"]] = dict(event)

    container = (stock or {}).get("events") or {}
    values = []
    latest = container.get("latest")
    if isinstance(latest, dict):
        values.append(latest)
    for key in ("recent", "upcoming"):
        rows = container.get(key)
        if isinstance(rows, list):
            values.extend(rows)
    for event in values:
        if isinstance(event, dict) and event.get("event_id"):
            old = by_id.get(event["event_id"], {})
            merged = dict(old)
            merged.update(event)
            by_id[event["event_id"]] = merged

    events = [event for event in by_id.values() if event.get("event_type") in TARGET_TYPES]
    events.sort(key=lambda item: (item.get("published_at") or "", item.get("event_id") or ""), reverse=True)
    return events, cache


def _coverage(stock, cache, as_of):
    container = (stock or {}).get("events") or {}
    cache_meta = container.get("cache") if isinstance(container.get("cache"), dict) else {}
    start = _date((cache or {}).get("covered_start_date") or cache_meta.get("covered_start_date"))
    complete = (cache or {}).get("query_status") == "OK"
    if cache is None:
        complete = bool(cache_meta.get("coverage_complete"))
    days = None
    if start:
        try:
            days = max(0, (date.fromisoformat(as_of) - date.fromisoformat(start)).days)
        except ValueError:
            days = None
    return {
        "covered_start_date": start,
        "coverage_days": days,
        "coverage_complete": bool(complete),
        "target_history_days": COVERAGE_TARGET_DAYS,
        "sufficient_for_persistent_state": bool(complete and days is not None and days >= COVERAGE_TARGET_DAYS),
        "source": "CNINFO_EVENT_CACHE" if cache is not None else "SNAPSHOT_EVENT_WINDOW",
    }


def _execution_window(event, facts, as_of):
    status = str((event or {}).get("status") or "").upper()
    progress = str((facts or {}).get("progress") or "").upper()
    if status in {"COMPLETED", "CANCELLED"} or progress == "COMPLETED":
        return "INACTIVE"
    if progress == "IN_PROGRESS":
        return "ACTIVE"

    start = _date(_first(facts, "plan_start_date", "period_start_date", "start_date", "execution_start_date"))
    end = _date(_first(facts, "plan_end_date", "period_end_date", "end_date", "deadline_date", "execution_end_date"))
    if start and end:
        current = date.fromisoformat(as_of)
        return "ACTIVE" if date.fromisoformat(start) <= current <= date.fromisoformat(end) else "INACTIVE"
    return "UNKNOWN"


def _remaining_range(minimum, maximum, completed):
    if completed is None:
        return None, None
    low = max(0.0, minimum - completed) if minimum is not None else None
    high = max(0.0, maximum - completed) if maximum is not None else None
    return core._round(low, 4) if low is not None else None, core._round(high, 4) if high is not None else None


def _plan_event(event, as_of):
    facts = event.get("facts") if isinstance(event.get("facts"), dict) else {}
    event_type = event.get("event_type")
    planned_amount_min = _number(facts, "amount_min_yuan", "planned_amount_min_yuan")
    planned_amount_max = _number(facts, "amount_max_yuan", "planned_amount_max_yuan")
    planned_shares_min = _number(facts, "planned_shares_min", "plan_shares_min")
    planned_shares_max = _number(facts, "planned_shares_max", "plan_shares_max")
    planned_shares = _number(facts, "planned_shares", "plan_shares")
    if planned_shares is not None:
        planned_shares_min = planned_shares_max = planned_shares

    completed_amount = _number(facts, "completed_amount_yuan", "repurchased_amount_yuan", "actual_amount_yuan")
    completed_shares = _number(facts, "completed_shares", "repurchased_shares", "actual_shares")
    remain_amount_min, remain_amount_max = _remaining_range(planned_amount_min, planned_amount_max, completed_amount)
    remain_shares_min, remain_shares_max = _remaining_range(planned_shares_min, planned_shares_max, completed_shares)
    explicit_percentages = facts.get("share_percentages") if isinstance(facts.get("share_percentages"), list) else []

    start_date = _date(_first(facts, "plan_start_date", "period_start_date", "start_date", "execution_start_date"))
    end_date = _date(_first(facts, "plan_end_date", "period_end_date", "end_date", "deadline_date", "execution_end_date"))
    window = _execution_window(event, facts, as_of)
    flags = []
    if window == "UNKNOWN" and str(event.get("status") or "").upper() not in {"COMPLETED", "CANCELLED"}:
        flags.append("EXECUTION_WINDOW_NOT_EXPLICIT")
    if event_type == "BUYBACK" and planned_amount_min is None and planned_amount_max is None:
        flags.append("PLANNED_AMOUNT_UNAVAILABLE")
    if event_type in {"HOLDER_INCREASE", "HOLDER_DECREASE"} and planned_shares_min is None and planned_shares_max is None:
        flags.append("PLANNED_SHARES_UNAVAILABLE")
    if event_type in {"HOLDER_INCREASE", "HOLDER_DECREASE"} and not explicit_percentages:
        flags.append("PLAN_PERCENTAGE_UNAVAILABLE")

    document = facts.get("document_extraction") if isinstance(facts.get("document_extraction"), dict) else {}
    return {
        "event_id": event.get("event_id"),
        "event_type": event_type,
        "status": event.get("status") or "UNKNOWN",
        "published_at": event.get("published_at"),
        "effective_date": event.get("effective_date"),
        "title": event.get("title"),
        "active_execution_window": window,
        "window_start_date": start_date,
        "window_end_date": end_date,
        "planned_amount_min_yuan": planned_amount_min,
        "planned_amount_max_yuan": planned_amount_max,
        "completed_amount_yuan": completed_amount,
        "remaining_amount_min_yuan": remain_amount_min,
        "remaining_amount_max_yuan": remain_amount_max,
        "planned_shares_min": planned_shares_min,
        "planned_shares_max": planned_shares_max,
        "completed_shares": completed_shares,
        "remaining_shares_min": remain_shares_min,
        "remaining_shares_max": remain_shares_max,
        "average_price_yuan_per_share": _number(facts, "average_price_yuan_per_share", "avg_price_yuan_per_share"),
        "price_cap_yuan_per_share": _number(facts, "price_cap_yuan_per_share"),
        "share_percentages": [core._as_float(value) for value in explicit_percentages if core._as_float(value) is not None],
        "related_event_id": event.get("related_event_id"),
        "supersedes_event_id": event.get("supersedes_event_id"),
        "quality_flags": flags,
        "provenance": {
            "provider": "CNINFO",
            "source_tier": event.get("source_tier") or "OFFICIAL",
            "source_document_id": event.get("source_document_id"),
            "source_url": event.get("source_url"),
            "fact_extraction_scope": facts.get("extraction_scope"),
            "document_extraction_status": document.get("status"),
        },
    }


def _plan_bucket(events, event_type, as_of):
    history = [_plan_event(event, as_of) for event in events if event.get("event_type") == event_type][:PLAN_HISTORY_LIMIT]
    current = history[0] if history else None
    return {
        "current": current,
        "confirmed_active": [item for item in history if item.get("active_execution_window") == "ACTIVE"],
        "nonterminal_unknown_window": [
            item for item in history
            if item.get("active_execution_window") == "UNKNOWN"
            and str(item.get("status") or "").upper() not in {"COMPLETED", "CANCELLED"}
        ],
        "history": history,
    }


def normalize_plans(events, coverage, as_of, fetched_at):
    buckets = {
        "buybacks": _plan_bucket(events, "BUYBACK", as_of),
        "holder_increase_plans": _plan_bucket(events, "HOLDER_INCREASE", as_of),
        "holder_decrease_plans": _plan_bucket(events, "HOLDER_DECREASE", as_of),
    }
    history = [item for bucket in buckets.values() for item in bucket["history"]]
    history.sort(key=lambda item: (item.get("published_at") or "", item.get("event_id") or ""), reverse=True)
    flags = []
    if not coverage.get("sufficient_for_persistent_state"):
        flags.append("EVENT_HISTORY_COVERAGE_INSUFFICIENT_FOR_PERSISTENT_STATE")
    if any(item.get("quality_flags") for item in history):
        flags.append("SOME_PLAN_FACTS_INCOMPLETE")
    status = "OK" if coverage.get("sufficient_for_persistent_state") and not flags else "PARTIAL"
    return {
        "status": status,
        "as_of_date": as_of,
        **buckets,
        "metadata": {
            "freshness": "OFFICIAL_EVENT_DERIVED_STATE",
            "realtime": False,
            "quality": "PASS" if status == "OK" else "PARTIAL",
            "quality_flags": flags,
            "coverage": coverage,
            "active_window_policy": (
                "ACTIVE only from explicit IN_PROGRESS fact or explicit start/end dates; "
                "OPEN announcement alone remains UNKNOWN"
            ),
            "remaining_policy": "derive only from explicit planned ranges and explicit completed values",
            "sentiment_policy": "STRUCTURAL_FACTS_ONLY; NO_BULLISH_BEARISH_INFERENCE",
        },
        "provenance": {
            "provider": "CNINFO",
            "source_tier": "OFFICIAL",
            "fetched_at": fetched_at,
            "event_types": list(TARGET_PLAN_TYPES),
            "source_contract": "company_events normalized official disclosure events and enriched facts",
        },
    }


def _unlock_event(event, as_of, share_values):
    facts = event.get("facts") if isinstance(event.get("facts"), dict) else {}
    published = _event_published_date(event)
    unlock_date = _date(event.get("effective_date"))
    if not unlock_date:
        candidate = _date(facts.get("unlock_date"))
        if candidate and (not published or candidate >= published):
            unlock_date = candidate
    shares = _number(facts, "unlock_shares", "shares_unlocked", "unlock_share_count")
    total = core._as_float((share_values or {}).get("total_shares"))
    floating = core._as_float((share_values or {}).get("float_shares"))
    total_ratio = core._ratio_percent(shares, total) if shares is not None else None
    float_ratio = core._ratio_percent(shares, floating) if shares is not None else None
    days_until = None
    if unlock_date:
        days_until = (date.fromisoformat(unlock_date) - date.fromisoformat(as_of)).days
    flags = []
    if unlock_date is None:
        flags.append("UNLOCK_DATE_UNAVAILABLE")
    if shares is None:
        flags.append("UNLOCK_SHARE_COUNT_UNAVAILABLE")
    if shares is not None and total in (None, 0):
        flags.append("TOTAL_SHARES_DENOMINATOR_UNAVAILABLE")
    if shares is not None and floating in (None, 0):
        flags.append("FLOAT_SHARES_DENOMINATOR_UNAVAILABLE")
    return {
        "event_id": event.get("event_id"),
        "published_at": event.get("published_at"),
        "unlock_date": unlock_date,
        "days_until": days_until,
        "status": event.get("status") or "UNKNOWN",
        "title": event.get("title"),
        "unlock_shares": shares,
        "unlock_ratio_total_percent": total_ratio,
        "unlock_ratio_float_percent": float_ratio,
        "provider_unscoped_percentages": [
            value for value in (core._as_float(v) for v in (facts.get("unlock_percentages") or [])) if value is not None
        ],
        "importance": event.get("importance"),
        "quality_flags": flags,
        "provenance": {
            "provider": "CNINFO",
            "source_tier": event.get("source_tier") or "OFFICIAL",
            "source_document_id": event.get("source_document_id"),
            "source_url": event.get("source_url"),
            "fact_extraction_scope": facts.get("extraction_scope"),
        },
    }


def normalize_unlocks(events, coverage, as_of, fetched_at, share_structure):
    values = (share_structure or {}).get("values") if isinstance(share_structure, dict) else {}
    history = [_unlock_event(event, as_of, values or {}) for event in events if event.get("event_type") == "UNLOCK"]
    history.sort(key=lambda item: (item.get("unlock_date") or "", item.get("published_at") or ""), reverse=True)
    history = history[:UNLOCK_HISTORY_LIMIT]
    upcoming = [item for item in history if item.get("days_until") is not None and item["days_until"] > 0]
    upcoming.sort(key=lambda item: (item.get("unlock_date") or "", item.get("published_at") or ""))
    windows = {
        f"{days}d": [item for item in upcoming if item.get("days_until") <= days]
        for days in WINDOWS
    }
    historical = [item for item in history if item.get("days_until") is not None and item["days_until"] <= 0]
    flags = []
    if not coverage.get("sufficient_for_persistent_state"):
        flags.append("EVENT_HISTORY_COVERAGE_INSUFFICIENT_FOR_PERSISTENT_STATE")
    if any(item.get("quality_flags") for item in history):
        flags.append("SOME_UNLOCK_FACTS_INCOMPLETE")
    status = "OK" if coverage.get("sufficient_for_persistent_state") and not flags else "PARTIAL"
    return {
        "status": status,
        "as_of_date": as_of,
        "current_restricted_shares": core._as_float((values or {}).get("restricted_shares")),
        "upcoming": upcoming,
        "upcoming_windows": windows,
        "history": historical,
        "high_importance_history": [item for item in historical if item.get("importance") == "HIGH"],
        "upcoming_event_refs": [
            {"event_id": item.get("event_id"), "effective_date": item.get("unlock_date")}
            for item in upcoming
        ],
        "metadata": {
            "freshness": "OFFICIAL_EVENT_DERIVED_STATE",
            "realtime": False,
            "quality": "PASS" if status == "OK" else "PARTIAL",
            "quality_flags": flags,
            "coverage": coverage,
            "window_semantics": "cumulative known unlock dates within 7/30/90/180 calendar days",
            "ratio_policy": "derive total/float ratios only when explicit unlock shares and dated share denominators exist",
            "unscoped_percentage_policy": "provider percentages retained but never assigned to total/float scope without evidence",
            "upcoming_events_reuse": "event_id + effective_date references are reusable by event/calendar layers",
        },
        "provenance": {
            "provider": "CNINFO",
            "source_tier": "OFFICIAL",
            "fetched_at": fetched_at,
            "event_type": "UNLOCK",
            "share_denominator_source": "ownership_and_capital.share_structure",
        },
    }


def _deferred_section(name, fetched_at):
    base = {
        "status": "DEFERRED",
        "as_of_date": None,
        "metadata": {
            "freshness": "NOT_DERIVED_IN_INTRADAY_FAST",
            "realtime": False,
            "quality": "PARTIAL",
            "quality_flags": [f"FULL_ONLY_{name.upper()}"],
        },
        "provenance": {"provider": "CNINFO", "source_tier": "OFFICIAL", "fetched_at": fetched_at},
    }
    if name == "buyback_and_holder_plans":
        base.update(
            buybacks={"current": None, "confirmed_active": [], "nonterminal_unknown_window": [], "history": []},
            holder_increase_plans={"current": None, "confirmed_active": [], "nonterminal_unknown_window": [], "history": []},
            holder_decrease_plans={"current": None, "confirmed_active": [], "nonterminal_unknown_window": [], "history": []},
        )
    else:
        base.update(
            current_restricted_shares=None,
            upcoming=[], upcoming_windows={f"{days}d": [] for days in WINDOWS},
            history=[], high_importance_history=[], upcoming_event_refs=[],
        )
    return base


def _context_status(context):
    names = (
        "share_structure", "controllers", "top_holders", "institutional_holdings",
        "shareholder_count", "buyback_and_holder_plans", "unlocks",
    )
    statuses = [(context.get(name) or {}).get("status") for name in names]
    if statuses and all(value == "DEFERRED" for value in statuses):
        return "DEFERRED"
    if statuses and all(value == "OK" for value in statuses):
        return "OK"
    if any(value in ("OK", "PARTIAL") for value in statuses):
        return "PARTIAL"
    return "UNAVAILABLE"


def extend_snapshot(snapshot_path, base, execution_mode):
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    fetched_at = core._runner_time_iso(snapshot)
    as_of = _as_of_date(snapshot)
    detail = snapshot.get("detail_stocks") or {}
    fast = str(execution_mode or "").upper() != "FULL"

    for code, stock in detail.items():
        context = stock.setdefault("ownership_and_capital", {})
        if fast:
            plans = _deferred_section("buyback_and_holder_plans", fetched_at)
            unlocks = _deferred_section("unlocks", fetched_at)
        else:
            events, cache = _events_for_stock(code, stock)
            coverage = _coverage(stock, cache, as_of)
            plans = normalize_plans(events, coverage, as_of, fetched_at)
            unlocks = normalize_unlocks(
                events, coverage, as_of, fetched_at, context.get("share_structure") or {}
            )
        context["buyback_and_holder_plans"] = plans
        context["unlocks"] = unlocks
        context["status"] = _context_status(context)
        print(
            "OWNERSHIP_EVENT_STATE "
            f"{code} plans_status={plans.get('status')} unlocks_status={unlocks.get('status')} "
            f"buybacks={len((plans.get('buybacks') or {}).get('history') or [])} "
            f"upcoming_unlocks={len(unlocks.get('upcoming') or [])}",
            flush=True,
        )

    summary = snapshot.setdefault("ownership_and_capital_summary", {})
    implemented = list(summary.get("implemented_sections") or [])
    for name in ("buyback_and_holder_plans", "unlocks"):
        if name not in implemented:
            implemented.append(name)
    summary["implemented_sections"] = implemented
    summary["buyback_and_holder_plans_contract"] = (
        "CNINFO_EVENT_STATE; OPEN!=ACTIVE; EXPLICIT_PROGRESS_OR_WINDOW_ONLY; FAIL_CLOSED_REMAINING"
    )
    summary["unlocks_contract"] = (
        "CNINFO_EVENT_STATE; 7_30_90_180D_CUMULATIVE_WINDOWS; EXPLICIT_SHARES_ONLY_FOR_RATIOS"
    )
    status_by_code = {
        code: detail[code]["ownership_and_capital"].get("status")
        for code in sorted(detail) if "ownership_and_capital" in detail[code]
    }
    summary["status_by_code"] = status_by_code
    summary["status"] = (
        "OK" if status_by_code and all(v == "OK" for v in status_by_code.values())
        else "DEFERRED" if status_by_code and all(v == "DEFERRED" for v in status_by_code.values())
        else "PARTIAL" if status_by_code else "UNAVAILABLE"
    )
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
