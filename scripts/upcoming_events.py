import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


UPCOMING_EVENTS_VERSION = "v1"
SNAPSHOT_SCHEMA_VERSION = 18
WINDOWS = (7, 30, 90)
CST = timezone(timedelta(hours=8))
PLAN_BUCKETS = {
    "buybacks": "BUYBACK",
    "holder_increase_plans": "HOLDER_INCREASE",
    "holder_decrease_plans": "HOLDER_DECREASE",
}


def _date(value):
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    text = text[:10]
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _as_of_date(snapshot):
    for key in ("runner_time_cst", "runner_time_utc"):
        value = _date(snapshot.get(key))
        if value:
            return value
    return datetime.now(CST).date().isoformat()


def _days_until(event_date, as_of):
    if not event_date:
        return None
    try:
        return (date.fromisoformat(event_date) - date.fromisoformat(as_of)).days
    except ValueError:
        return None


def _clean_source(source):
    if not isinstance(source, dict):
        return None
    value = {
        "provider": source.get("provider") or source.get("source"),
        "source_tier": source.get("source_tier"),
        "source_document_id": source.get("source_document_id"),
        "source_url": source.get("source_url"),
        "source_layer": source.get("source_layer"),
    }
    return {key: item for key, item in value.items() if item not in (None, "")}


def _source_relation(layer, source_event_id, source):
    value = _clean_source(source) or {}
    value["source_layer"] = layer
    if source_event_id:
        value["source_event_id"] = source_event_id
    return value


def _normalized_event(source_event_id, event_type, title, event_date, importance, source_relation, details):
    return {
        "event_id": f"upcoming:{source_event_id or event_type}:{event_type}:{event_date}",
        "event_type": event_type,
        "title": title,
        "event_date": event_date,
        "date_end": None,
        "date_certainty": "CONFIRMED_DATE",
        "days_until_event": None,
        "importance": importance or "MEDIUM",
        "status": "UPCOMING",
        "source_event_id": source_event_id,
        "source_relations": [source_relation] if source_relation else [],
        "details": details or {},
    }


def _company_unlock_candidates(stock, as_of):
    emitted = []
    excluded_unproven = 0
    container = (stock or {}).get("events")
    if not isinstance(container, dict):
        return emitted, excluded_unproven, "UNAVAILABLE"

    rows = container.get("upcoming")
    if not isinstance(rows, list):
        rows = []
    for event in rows:
        if not isinstance(event, dict):
            continue
        if str(event.get("status") or "").upper() in {"COMPLETED", "CANCELLED"}:
            continue

        event_type = event.get("event_type")
        facts = event.get("facts") if isinstance(event.get("facts"), dict) else {}
        if event_type != "UNLOCK":
            if event.get("effective_date"):
                excluded_unproven += 1
            continue

        event_date = _date(facts.get("unlock_date"))
        effective_date = _date(event.get("effective_date"))
        if not event_date or (effective_date and effective_date != event_date):
            excluded_unproven += 1
            continue
        days = _days_until(event_date, as_of)
        if days is None or days < 0:
            continue

        source_event_id = event.get("event_id")
        relation = _source_relation(
            "company_events",
            source_event_id,
            {
                "provider": event.get("source"),
                "source_tier": event.get("source_tier"),
                "source_document_id": event.get("source_document_id"),
                "source_url": event.get("source_url"),
            },
        )
        emitted.append(
            _normalized_event(
                source_event_id,
                "UNLOCK",
                event.get("title"),
                event_date,
                event.get("importance") or "MEDIUM",
                relation,
                {
                    "fact_extraction_scope": facts.get("extraction_scope"),
                },
            )
        )
    return emitted, excluded_unproven, container.get("status") or "UNKNOWN"


def _ownership_unlock_candidates(stock, as_of):
    context = (stock or {}).get("ownership_and_capital")
    if not isinstance(context, dict):
        return [], "UNAVAILABLE"
    unlocks = context.get("unlocks")
    if not isinstance(unlocks, dict):
        return [], "UNAVAILABLE"

    emitted = []
    for item in unlocks.get("upcoming") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").upper() in {"COMPLETED", "CANCELLED"}:
            continue
        event_date = _date(item.get("unlock_date"))
        days = _days_until(event_date, as_of)
        if days is None or days < 0:
            continue
        source_event_id = item.get("event_id")
        relation = _source_relation(
            "ownership_and_capital.unlocks",
            source_event_id,
            item.get("provenance") or {},
        )
        emitted.append(
            _normalized_event(
                source_event_id,
                "UNLOCK",
                item.get("title"),
                event_date,
                item.get("importance") or "MEDIUM",
                relation,
                {
                    "unlock_shares": item.get("unlock_shares"),
                    "unlock_ratio_total_percent": item.get("unlock_ratio_total_percent"),
                    "unlock_ratio_float_percent": item.get("unlock_ratio_float_percent"),
                },
            )
        )
    return emitted, unlocks.get("status") or "UNKNOWN"


def _ownership_plan_candidates(stock, as_of):
    context = (stock or {}).get("ownership_and_capital")
    if not isinstance(context, dict):
        return [], "UNAVAILABLE"
    plans = context.get("buyback_and_holder_plans")
    if not isinstance(plans, dict):
        return [], "UNAVAILABLE"

    emitted = []
    for bucket_name, base_type in PLAN_BUCKETS.items():
        bucket = plans.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        for item in bucket.get("history") or []:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").upper()
            if status in {"COMPLETED", "CANCELLED"}:
                continue
            source_event_id = item.get("event_id")
            relation = _source_relation(
                f"ownership_and_capital.buyback_and_holder_plans.{bucket_name}",
                source_event_id,
                item.get("provenance") or {},
            )
            for field, suffix, label in (
                ("window_start_date", "WINDOW_START", "execution window starts"),
                ("window_end_date", "WINDOW_END", "execution window ends"),
            ):
                event_date = _date(item.get(field))
                days = _days_until(event_date, as_of)
                if days is None or days < 0:
                    continue
                emitted.append(
                    _normalized_event(
                        source_event_id,
                        f"{base_type}_{suffix}",
                        f"{item.get('title') or base_type}: {label}",
                        event_date,
                        "MEDIUM",
                        relation,
                        {
                            "plan_event_type": base_type,
                            "active_execution_window": item.get("active_execution_window"),
                            "plan_status": item.get("status"),
                        },
                    )
                )
    return emitted, plans.get("status") or "UNKNOWN"


def _merge_sources(existing, incoming):
    seen = {
        (
            item.get("source_layer"),
            item.get("source_event_id"),
            item.get("source_document_id"),
            item.get("source_url"),
        )
        for item in existing
        if isinstance(item, dict)
    }
    for item in incoming:
        if not isinstance(item, dict):
            continue
        key = (
            item.get("source_layer"),
            item.get("source_event_id"),
            item.get("source_document_id"),
            item.get("source_url"),
        )
        if key not in seen:
            existing.append(item)
            seen.add(key)


def _dedupe(events):
    merged = {}
    for event in events:
        source_event_id = event.get("source_event_id")
        key = (
            source_event_id or event.get("event_id"),
            event.get("event_type"),
            event.get("event_date"),
        )
        current = merged.get(key)
        if current is None:
            merged[key] = dict(event)
            continue
        _merge_sources(current.setdefault("source_relations", []), event.get("source_relations") or [])
        details = current.setdefault("details", {})
        for name, value in (event.get("details") or {}).items():
            if details.get(name) is None and value is not None:
                details[name] = value
    return list(merged.values())


def _window_name(days_until):
    if days_until <= 7:
        return "next_7d"
    if days_until <= 30:
        return "next_30d"
    if days_until <= 90:
        return "next_90d"
    return "later"


def build_upcoming_events(stock, as_of):
    company, excluded, company_status = _company_unlock_candidates(stock, as_of)
    unlocks, unlock_status = _ownership_unlock_candidates(stock, as_of)
    plans, plan_status = _ownership_plan_candidates(stock, as_of)
    events = _dedupe(company + unlocks + plans)

    for event in events:
        event["days_until_event"] = _days_until(event.get("event_date"), as_of)
    events = [
        event
        for event in events
        if event.get("days_until_event") is not None and event["days_until_event"] >= 0
    ]
    events.sort(key=lambda item: (item.get("event_date") or "", item.get("event_type") or "", item.get("event_id") or ""))

    windows = {"next_7d": [], "next_30d": [], "next_90d": [], "later": []}
    for event in events:
        windows[_window_name(event["days_until_event"])].append(event)

    high = [event for event in events if event.get("importance") == "HIGH"]
    source_status = {
        "company_events": company_status,
        "ownership_unlocks": unlock_status,
        "ownership_plans": plan_status,
    }
    any_source = any(value not in {"UNAVAILABLE", None} for value in source_status.values())
    partial_source = any(value in {"PARTIAL", "DEGRADED", "DEFERRED", "UNKNOWN"} for value in source_status.values())
    status = "OK" if any_source and not partial_source else "PARTIAL" if any_source else "UNAVAILABLE"
    return {
        "status": status,
        "as_of_date": as_of,
        "nearest": events[0] if events else None,
        **windows,
        "calendar_summary": {
            "event_count": len(events),
            "next_7d_event_count": len(windows["next_7d"]),
            "next_30d_event_count": sum(1 for event in events if event["days_until_event"] <= 30),
            "next_30d_high_importance_count": sum(
                1
                for event in events
                if event["days_until_event"] <= 30 and event.get("importance") == "HIGH"
            ),
            "nearest_high_importance_event_days": high[0]["days_until_event"] if high else None,
        },
        "metadata": {
            "freshness": "DERIVED_FROM_CURRENT_SNAPSHOT_FACTS",
            "realtime": False,
            "quality": "PASS" if status == "OK" else "PARTIAL" if status == "PARTIAL" else "FAILED",
            "source_status": source_status,
            "date_certainty_policy": (
                "emit only type-scoped explicit future dates; generic announcement dates are excluded"
            ),
            "window_semantics": "non-overlapping calendar-day buckets: 0-7, 8-30, 31-90, >90",
            "dedupe_policy": "same source_event_id + normalized event_type + event_date",
            "excluded_unproven_company_event_count": excluded,
            "interpretation_scope": "CONTEXT_ONLY; NO_BULLISH_BEARISH_INFERENCE",
        },
        "provenance": {
            "source_layers": [
                "detail_stocks.<code>.events.upcoming",
                "detail_stocks.<code>.ownership_and_capital.unlocks",
                "detail_stocks.<code>.ownership_and_capital.buyback_and_holder_plans",
            ],
            "derived_at_snapshot_as_of": as_of,
        },
    }


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    as_of = _as_of_date(snapshot)
    detail = snapshot.get("detail_stocks") or {}
    status_by_code = {}

    for code, stock in detail.items():
        value = build_upcoming_events(stock, as_of)
        stock["upcoming_events"] = value
        status_by_code[code] = value.get("status")

    snapshot["schema_version"] = max(int(snapshot.get("schema_version") or 0), SNAPSHOT_SCHEMA_VERSION)
    snapshot.setdefault("features", {})["upcoming_events"] = UPCOMING_EVENTS_VERSION
    snapshot["upcoming_events_summary"] = {
        "status": (
            "OK"
            if status_by_code and all(value == "OK" for value in status_by_code.values())
            else "PARTIAL"
            if status_by_code
            else "UNAVAILABLE"
        ),
        "detail_stock_count": len(status_by_code),
        "status_by_code": dict(sorted(status_by_code.items())),
        "implemented_sources": [
            "official_company_unlock_dates",
            "ownership_unlock_state",
            "ownership_plan_explicit_windows",
        ],
        "date_policy": "TYPE_SCOPED_EXPLICIT_DATES_ONLY",
    }
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "UPCOMING_EVENTS "
        f"status={snapshot['upcoming_events_summary']['status']} detail_stocks={len(status_by_code)}",
        flush=True,
    )
    print(
        f"SNAPSHOT_SCHEMA_UPGRADED schema_version={SNAPSHOT_SCHEMA_VERSION} "
        f"feature=upcoming_events:{UPCOMING_EVENTS_VERSION}",
        flush=True,
    )
