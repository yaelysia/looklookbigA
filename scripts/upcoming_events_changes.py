import json
from pathlib import Path


WINDOW_BUCKETS = ("next_7d", "next_30d", "next_90d", "later")
TERMINAL_CANCELLED = {"CANCELLED", "CANCELED"}
TERMINAL_COMPLETED = {"COMPLETED", "CLOSED", "DONE", "FINISHED"}
SEVERITY_ORDER = {"NONE": 0, "MINOR": 1, "MODERATE": 2, "SIGNIFICANT": 3}


def _layer(stock):
    value = (stock or {}).get("upcoming_events")
    return value if isinstance(value, dict) else None


def _comparable(layer):
    return isinstance(layer, dict) and str(layer.get("status") or "").upper() in {"OK", "PARTIAL"}


def _logical_key(event):
    event_type = str(event.get("event_type") or "UNKNOWN")
    source_event_id = event.get("source_event_id")
    if source_event_id:
        return f"source:{source_event_id}|type:{event_type}"
    event_id = event.get("event_id")
    if event_id:
        # Event ids may embed the event date. Keep the event type separate and
        # strip only the final :YYYY-MM-DD component when it is present so a
        # confirmed date correction remains comparable rather than looking new.
        text = str(event_id)
        parts = text.rsplit(":", 1)
        if len(parts) == 2 and len(parts[1]) == 10 and parts[1][4:5] == "-" and parts[1][7:8] == "-":
            text = parts[0]
        return f"event:{text}|type:{event_type}"
    return f"fallback:{event_type}|title:{str(event.get('title') or '').strip()}"


def _event_map(layer):
    result = {}
    if not isinstance(layer, dict):
        return result
    for bucket in WINDOW_BUCKETS:
        rows = layer.get(bucket)
        if not isinstance(rows, list):
            continue
        for event in rows:
            if not isinstance(event, dict):
                continue
            key = _logical_key(event)
            value = dict(event)
            value["window_bucket"] = bucket
            current = result.get(key)
            if current is None:
                result[key] = value
            else:
                # Fail closed on accidental duplicates: keep the earliest event
                # date but preserve a marker rather than silently choosing a
                # semantically different duplicate.
                current_date = current.get("event_date") or "9999-12-31"
                incoming_date = value.get("event_date") or "9999-12-31"
                if incoming_date < current_date:
                    result[key] = value
                result[key]["logical_duplicate_detected"] = True
    return result


def _terminal_source_status(stock, source_event_id):
    if not source_event_id:
        return None

    def inspect_event(event):
        if not isinstance(event, dict) or str(event.get("event_id") or "") != str(source_event_id):
            return None
        status = str(event.get("status") or "").upper()
        return status or None

    events = (stock or {}).get("events")
    if isinstance(events, dict):
        for key in ("recent", "upcoming", "latest"):
            rows = events.get(key)
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list):
                continue
            for event in rows:
                status = inspect_event(event)
                if status:
                    return status
    elif isinstance(events, list):
        for event in events:
            status = inspect_event(event)
            if status:
                return status

    ownership = (stock or {}).get("ownership_and_capital") or {}
    plans = ownership.get("buyback_and_holder_plans") or {}
    for bucket_name in ("buybacks", "holder_increase_plans", "holder_decrease_plans"):
        bucket = plans.get(bucket_name) or {}
        for event in bucket.get("history") or []:
            status = inspect_event(event)
            if status:
                return status
    return None


def _crossed_threshold(before_bucket, after_bucket, threshold_bucket):
    rank = {"next_7d": 0, "next_30d": 1, "next_90d": 2, "later": 3}
    if before_bucket not in rank or after_bucket not in rank or threshold_bucket not in rank:
        return False
    threshold_rank = rank[threshold_bucket]
    return rank[before_bucket] > threshold_rank and rank[after_bucket] <= threshold_rank


def _event_entry(key, event):
    return {
        "logical_key": key,
        "event_id": event.get("event_id"),
        "source_event_id": event.get("source_event_id"),
        "event_type": event.get("event_type"),
        "title": event.get("title"),
        "event_date": event.get("event_date"),
        "date_end": event.get("date_end"),
        "date_certainty": event.get("date_certainty"),
        "importance": event.get("importance"),
        "status": event.get("status"),
        "window_bucket": event.get("window_bucket"),
    }


def build_stock_changes(before_stock, after_stock):
    before_layer = _layer(before_stock)
    after_layer = _layer(after_stock)
    if before_layer is None and after_layer is None:
        return {
            "status": "NO_LAYER",
            "new": [],
            "removed": [],
            "date_changed": [],
            "status_changed": [],
            "window_transitions": [],
            "changed": False,
            "significance": "NONE",
            "quality_flags": [],
        }
    if not _comparable(before_layer) or not _comparable(after_layer):
        flags = []
        if not _comparable(before_layer):
            flags.append("BASELINE_UPCOMING_EVENTS_NOT_COMPARABLE")
        if not _comparable(after_layer):
            flags.append("CURRENT_UPCOMING_EVENTS_NOT_COMPARABLE")
        return {
            "status": "NO_COMPARABLE_BASELINE",
            "new": [],
            "removed": [],
            "date_changed": [],
            "status_changed": [],
            "window_transitions": [],
            "changed": False,
            "significance": "NONE",
            "quality_flags": flags,
        }

    before_map = _event_map(before_layer)
    after_map = _event_map(after_layer)
    new = []
    removed = []
    date_changed = []
    status_changed = []
    window_transitions = []

    for key in sorted(set(before_map) | set(after_map)):
        before = before_map.get(key)
        after = after_map.get(key)
        if before is None:
            entry = _event_entry(key, after)
            entry["kind"] = "NEW_UPCOMING_EVENT"
            new.append(entry)
            continue
        if after is None:
            entry = _event_entry(key, before)
            source_status = _terminal_source_status(after_stock, before.get("source_event_id"))
            if source_status in TERMINAL_CANCELLED:
                entry["kind"] = "EVENT_CANCELLED"
                entry["terminal_status"] = source_status
                status_changed.append(entry)
            elif source_status in TERMINAL_COMPLETED:
                entry["kind"] = "EVENT_COMPLETED"
                entry["terminal_status"] = source_status
                status_changed.append(entry)
            else:
                entry["kind"] = "EVENT_REMOVED_FROM_UPCOMING"
                removed.append(entry)
            continue

        if before.get("event_date") != after.get("event_date") or before.get("date_end") != after.get("date_end"):
            date_changed.append(
                {
                    "kind": "EVENT_DATE_CHANGED",
                    "logical_key": key,
                    "event_type": after.get("event_type") or before.get("event_type"),
                    "title": after.get("title") or before.get("title"),
                    "before_event_date": before.get("event_date"),
                    "after_event_date": after.get("event_date"),
                    "before_date_end": before.get("date_end"),
                    "after_date_end": after.get("date_end"),
                    "source_event_id": after.get("source_event_id") or before.get("source_event_id"),
                    "importance": after.get("importance") or before.get("importance"),
                }
            )

        before_status = str(before.get("status") or "").upper()
        after_status = str(after.get("status") or "").upper()
        if before_status != after_status:
            status_changed.append(
                {
                    "kind": "EVENT_STATUS_CHANGED",
                    "logical_key": key,
                    "event_type": after.get("event_type") or before.get("event_type"),
                    "title": after.get("title") or before.get("title"),
                    "before_status": before.get("status"),
                    "after_status": after.get("status"),
                    "source_event_id": after.get("source_event_id") or before.get("source_event_id"),
                    "importance": after.get("importance") or before.get("importance"),
                }
            )

        before_bucket = before.get("window_bucket")
        after_bucket = after.get("window_bucket")
        for threshold_bucket, kind in (
            ("next_30d", "ENTERED_30D_WINDOW"),
            ("next_7d", "ENTERED_7D_WINDOW"),
        ):
            if _crossed_threshold(before_bucket, after_bucket, threshold_bucket):
                window_transitions.append(
                    {
                        "kind": kind,
                        "logical_key": key,
                        "event_type": after.get("event_type") or before.get("event_type"),
                        "title": after.get("title") or before.get("title"),
                        "event_date": after.get("event_date"),
                        "source_event_id": after.get("source_event_id") or before.get("source_event_id"),
                        "importance": after.get("importance") or before.get("importance"),
                        "before_window": before_bucket,
                        "after_window": after_bucket,
                    }
                )

    changed = bool(new or removed or date_changed or status_changed or window_transitions)
    important = any(
        str(item.get("importance") or "").upper() == "HIGH"
        for collection in (new, removed, date_changed, status_changed)
        for item in collection
    )
    significance = "SIGNIFICANT" if important else "MODERATE" if changed else "NONE"
    return {
        "status": "AVAILABLE",
        "new": new,
        "removed": removed,
        "date_changed": date_changed,
        "status_changed": status_changed,
        "window_transitions": window_transitions,
        "changed": changed,
        "significance": significance,
        "comparison_policy": (
            "logical source event + event type; compare material date/status semantics; "
            "emit only 30d/7d inward threshold crossings; classify removal as completed/cancelled only with current source-state evidence"
        ),
        "quality_flags": [],
    }


def build_changes(previous, current):
    previous_detail = (previous or {}).get("detail_stocks") or {}
    current_detail = (current or {}).get("detail_stocks") or {}
    by_stock = {}
    aggregate = {
        "new": [],
        "removed": [],
        "date_changed": [],
        "status_changed": [],
        "window_transitions": [],
    }
    any_layer = False
    any_comparable = False
    significance = "NONE"

    for code in sorted(set(previous_detail) | set(current_detail)):
        stock_change = build_stock_changes(previous_detail.get(code), current_detail.get(code))
        if stock_change["status"] != "NO_LAYER":
            any_layer = True
        if stock_change["status"] == "AVAILABLE":
            any_comparable = True
        if stock_change.get("changed"):
            by_stock[code] = stock_change
            for name in aggregate:
                for item in stock_change.get(name) or []:
                    aggregate[name].append({"code": code, **item})
            if SEVERITY_ORDER[stock_change.get("significance", "NONE")] > SEVERITY_ORDER[significance]:
                significance = stock_change["significance"]

    if not any_layer:
        status = "NO_LAYER"
    elif not any_comparable:
        status = "NO_COMPARABLE_BASELINE"
    else:
        status = "AVAILABLE"
    return {
        "status": status,
        **aggregate,
        "by_stock": by_stock,
        "significance": significance,
    }


def _recount_summary(changes):
    significant = 0
    moderate = 0
    minor = 0
    items = list((changes.get("stocks") or {}).values())
    items += list((changes.get("groups") or {}).values())
    for item in (changes.get("market"), changes.get("events")):
        if isinstance(item, dict):
            items.append(item)
    for item in items:
        severity = str((item or {}).get("significance") or "NONE").upper()
        if severity == "SIGNIFICANT":
            significant += 1
        elif severity == "MODERATE":
            moderate += 1
        elif severity == "MINOR":
            minor += 1
    summary = changes.setdefault("summary", {})
    summary["significant_changes"] = significant
    summary["moderate_changes"] = moderate
    summary["minor_changes"] = minor


def apply_to_changes(previous, current, changes):
    upcoming = build_changes(previous, current)
    changes["upcoming_events"] = upcoming
    stocks = changes.setdefault("stocks", {})
    for code, stock_change in upcoming.get("by_stock", {}).items():
        target = stocks.setdefault(code, {"code": code, "significance": "NONE", "significance_reasons": []})
        target["upcoming_events_change"] = stock_change
        severity = stock_change.get("significance") or "MODERATE"
        if SEVERITY_ORDER.get(severity, 0) > SEVERITY_ORDER.get(target.get("significance") or "NONE", 0):
            target["significance"] = severity
        target.setdefault("significance_reasons", []).append(
            {
                "severity": severity,
                "reason": "UPCOMING_EVENTS_CHANGED",
                "value": {
                    "new": len(stock_change.get("new") or []),
                    "removed": len(stock_change.get("removed") or []),
                    "date_changed": len(stock_change.get("date_changed") or []),
                    "status_changed": len(stock_change.get("status_changed") or []),
                    "window_transitions": len(stock_change.get("window_transitions") or []),
                },
            }
        )

    summary = changes.setdefault("summary", {})
    summary["new_upcoming_events"] = len(upcoming.get("new") or [])
    summary["removed_upcoming_events"] = len(upcoming.get("removed") or [])
    summary["upcoming_event_date_changes"] = len(upcoming.get("date_changed") or [])
    summary["upcoming_event_status_changes"] = len(upcoming.get("status_changed") or [])
    summary["upcoming_events_entered_30d"] = sum(
        1 for item in upcoming.get("window_transitions") or [] if item.get("kind") == "ENTERED_30D_WINDOW"
    )
    summary["upcoming_events_entered_7d"] = sum(
        1 for item in upcoming.get("window_transitions") or [] if item.get("kind") == "ENTERED_7D_WINDOW"
    )
    _recount_summary(changes)
    return changes


def finalize_snapshot(snapshot_path):
    import history_store

    path = Path(snapshot_path)
    current = json.loads(path.read_text(encoding="utf-8"))
    changes = current.get("changes_since_previous")
    if not isinstance(changes, dict):
        return
    previous, _ = history_store.load_previous_snapshot(current)
    if not isinstance(previous, dict):
        changes["upcoming_events"] = {
            "status": "NO_BASELINE",
            "new": [],
            "removed": [],
            "date_changed": [],
            "status_changed": [],
            "window_transitions": [],
            "by_stock": {},
            "significance": "NONE",
        }
        summary = changes.setdefault("summary", {})
        for name in (
            "new_upcoming_events",
            "removed_upcoming_events",
            "upcoming_event_date_changes",
            "upcoming_event_status_changes",
            "upcoming_events_entered_30d",
            "upcoming_events_entered_7d",
        ):
            summary[name] = 0
    else:
        apply_to_changes(previous, current, changes)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = changes.get("summary") or {}
    print(
        "UPCOMING_EVENTS_CHANGES "
        f"status={((changes.get('upcoming_events') or {}).get('status'))} "
        f"new={summary.get('new_upcoming_events')} date_changed={summary.get('upcoming_event_date_changes')} "
        f"status_changed={summary.get('upcoming_event_status_changes')} "
        f"entered_30d={summary.get('upcoming_events_entered_30d')} entered_7d={summary.get('upcoming_events_entered_7d')}",
        flush=True,
    )
