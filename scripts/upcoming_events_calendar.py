import json
import re
from datetime import date
from pathlib import Path

import market_calendar
import upcoming_events


_TITLE_CN_DATE_RE = re.compile(r"(?<!\d)(20\d{2})年(\d{1,2})月(\d{1,2})日")
_TITLE_ISO_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_TITLE_CANONICAL_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def _title_dates(title):
    values = []
    text = str(title or "")
    for pattern in (_TITLE_CN_DATE_RE, _TITLE_ISO_DATE_RE):
        for match in pattern.finditer(text):
            try:
                value = date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
            except ValueError:
                continue
            if value not in values:
                values.append(value)
    return values


def _title_labeled_date(title, labels):
    text = str(title or "")
    label = "(?:" + "|".join(re.escape(item) for item in labels) + ")"
    patterns = (
        re.compile(label + r"[^0-9]{0,8}(20\d{2})年(\d{1,2})月(\d{1,2})日"),
        re.compile(label + r"[^0-9]{0,8}(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            return None
    return None


def _title_scoped_dates(event):
    """Dates are emitted only when the announcement title itself fixes the date meaning."""
    title = str(event.get("title") or "")
    source_type = str(event.get("event_type") or "")
    values = []

    if source_type == "DIVIDEND":
        for labels, event_type, field in (
            (("股权登记日", "登记日"), "DIVIDEND_RECORD_DATE", "record_date"),
            (("除权除息日", "除息日", "除权日"), "DIVIDEND_EX_DATE", "ex_dividend_date"),
            (("现金红利发放日", "红利发放日"), "DIVIDEND_PAYMENT_DATE", "payment_date"),
        ):
            event_date = _title_labeled_date(title, labels)
            if event_date:
                values.append((event_type, event_date, field))

    if source_type == "SUSPENSION_RESUMPTION" and "复牌" in title:
        event_date = _title_labeled_date(title, ("复牌日", "复牌日期", "复牌时间"))
        if not event_date:
            match = re.search(
                r"(?:将于|自|于)\s*((?:20\d{2}年\d{1,2}月\d{1,2}日)|(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}))[^。；，,]{0,12}复牌",
                title,
            )
            if match:
                dates = _title_dates(match.group(1))
                event_date = dates[0] if dates else None
        if event_date:
            values.append(("RESUMPTION", event_date, "resumption_date"))

    if "股东大会" in title:
        event_date = _title_labeled_date(title, ("召开日期", "召开时间", "会议日期", "会议时间"))
        if not event_date:
            dates = _title_dates(title)
            if len(dates) == 1 and any(word in title for word in ("召开", "举行")):
                event_date = dates[0]
        if event_date:
            values.append(("SHAREHOLDER_MEETING", event_date, "meeting_date"))

    if source_type in {"PERIODIC_REPORT", "EARNINGS_EXPRESS", "EARNINGS_FORECAST"}:
        event_date = _title_labeled_date(
            title,
            ("披露日期", "披露日", "预约披露日", "预约披露日期"),
        )
        if event_date:
            values.append(("EARNINGS_RELEASE", event_date, "disclosure_date"))

    deduped = []
    seen = set()
    for item in values:
        key = item[:2]
        if key not in seen:
            deduped.append(item)
            seen.add(key)
    return deduped


def _company_title_candidates(stock, as_of):
    container = (stock or {}).get("events")
    if not isinstance(container, dict):
        return []
    rows = container.get("upcoming") if isinstance(container.get("upcoming"), list) else []
    emitted = []
    for event in rows:
        if not isinstance(event, dict):
            continue
        if str(event.get("status") or "").upper() in {"COMPLETED", "CANCELLED"}:
            continue
        source_event_id = event.get("event_id")
        relation = upcoming_events._source_relation(
            "company_events",
            source_event_id,
            {
                "provider": event.get("source"),
                "source_tier": event.get("source_tier"),
                "source_document_id": event.get("source_document_id"),
                "source_url": event.get("source_url"),
            },
        )
        for normalized_type, event_date, semantic_field in _title_scoped_dates(event):
            days = upcoming_events._days_until(event_date, as_of)
            if days is None or days < 0:
                continue
            emitted.append(
                upcoming_events._normalized_event(
                    source_event_id,
                    normalized_type,
                    event.get("title"),
                    event_date,
                    event.get("importance") or "MEDIUM",
                    relation,
                    {
                        "source_event_type": event.get("event_type"),
                        "date_semantics_source": "ANNOUNCEMENT_TITLE",
                        "semantic_field": semantic_field,
                    },
                )
            )
    return emitted


def _canonical_title(value):
    return _TITLE_CANONICAL_RE.sub("", str(value or "").lower())


def _aliases(event):
    event_type = event.get("event_type")
    event_date = event.get("event_date")
    values = []
    if event.get("source_event_id"):
        values.append(("source_event", event.get("source_event_id"), event_type, event_date))
    for relation in event.get("source_relations") or []:
        if not isinstance(relation, dict):
            continue
        document_id = relation.get("source_document_id")
        if document_id:
            values.append(
                (
                    "source_document",
                    relation.get("provider"),
                    document_id,
                    event_type,
                    event_date,
                )
            )
    title = _canonical_title(event.get("title"))
    if title:
        values.append(("exact_title", title, event_type, event_date))
    values.append(("event_id", event.get("event_id")))
    return values


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


def _merge_event(target, incoming):
    _merge_sources(target.setdefault("source_relations", []), incoming.get("source_relations") or [])
    details = target.setdefault("details", {})
    for name, value in (incoming.get("details") or {}).items():
        if details.get(name) is None and value is not None:
            details[name] = value


def _semantic_dedupe(events):
    groups = []
    alias_to_index = {}
    for event in events:
        aliases = _aliases(event)
        indexes = {alias_to_index[alias] for alias in aliases if alias in alias_to_index}
        if indexes:
            index = min(indexes)
            current = groups[index]
            _merge_event(current, event)
        else:
            index = len(groups)
            current = dict(event)
            groups.append(current)

        for other_index in sorted(indexes - {index}, reverse=True):
            other = groups[other_index]
            if other is None:
                continue
            _merge_event(current, other)
            groups[other_index] = None
            for alias, alias_index in list(alias_to_index.items()):
                if alias_index == other_index:
                    alias_to_index[alias] = index

        for alias in aliases + _aliases(current):
            alias_to_index[alias] = index
    return [event for event in groups if event is not None]


def _trading_day_context(event_date):
    value = {
        "verification_status": "UNVERIFIED",
        "is_trading_day": None,
        "reason": None,
        "previous_trading_day": None,
        "next_trading_day": None,
        "calendar_days_from_previous_trading_day": None,
        "calendar_days_to_next_trading_day": None,
        "nearest_trading_day_distance_calendar_days": None,
        "near_trading_day": None,
        "near_definition": "verified event day or within 1 calendar day of a verified trading day",
    }
    try:
        calendar = market_calendar.load_calendar()
        verification = market_calendar.trading_day_verification(event_date, calendar)
    except Exception as exc:
        value["reason"] = f"CALENDAR_ERROR:{type(exc).__name__}"
        return value

    value["verification_status"] = verification.get("verification_status") or "UNVERIFIED"
    value["is_trading_day"] = verification.get("is_trading_day")
    value["reason"] = verification.get("reason")
    if value["verification_status"] != "VERIFIED":
        return value

    try:
        target = date.fromisoformat(event_date)
        previous = market_calendar.previous_trading_day(target, calendar)
        following = market_calendar.next_trading_day(target, calendar)
    except Exception as exc:
        value["verification_status"] = "UNVERIFIED"
        value["is_trading_day"] = None
        value["reason"] = f"CALENDAR_NEIGHBOR_ERROR:{type(exc).__name__}"
        return value

    value["previous_trading_day"] = previous.isoformat() if previous else None
    value["next_trading_day"] = following.isoformat() if following else None
    if previous:
        value["calendar_days_from_previous_trading_day"] = (target - previous).days
    if following:
        value["calendar_days_to_next_trading_day"] = (following - target).days
    distances = [
        distance
        for distance in (
            0 if value["is_trading_day"] is True else None,
            value["calendar_days_from_previous_trading_day"],
            value["calendar_days_to_next_trading_day"],
        )
        if distance is not None and distance >= 0
    ]
    if distances:
        nearest = min(distances)
        value["nearest_trading_day_distance_calendar_days"] = nearest
        value["near_trading_day"] = nearest <= 1
    return value


def _all_window_events(calendar):
    events = []
    seen = set()
    for bucket in ("next_7d", "next_30d", "next_90d", "later"):
        for event in calendar.get(bucket) or []:
            if not isinstance(event, dict):
                continue
            key = event.get("event_id") or id(event)
            if key not in seen:
                events.append(dict(event))
                seen.add(key)
    return events


def _attach_overlap(events):
    by_date = {}
    for event in events:
        by_date.setdefault(event.get("event_date"), []).append(event)
    for same_day in by_date.values():
        high_count = sum(1 for event in same_day if event.get("importance") == "HIGH")
        for event in same_day:
            event["overlap_context"] = {
                "same_day_event_count": len(same_day),
                "same_day_high_importance_event_count": high_count,
                "overlaps_other_event": len(same_day) > 1,
                "overlaps_high_importance_event": high_count > (
                    1 if event.get("importance") == "HIGH" else 0
                ),
            }
    return by_date


def augment_stock(stock, as_of):
    calendar = stock.get("upcoming_events")
    if not isinstance(calendar, dict):
        return None
    existing = _all_window_events(calendar)
    title_candidates = _company_title_candidates(stock, as_of)
    events = _semantic_dedupe(existing + title_candidates)
    for event in events:
        event["days_until_event"] = upcoming_events._days_until(event.get("event_date"), as_of)
    events = [
        event
        for event in events
        if event.get("days_until_event") is not None and event["days_until_event"] >= 0
    ]
    events.sort(
        key=lambda event: (
            event.get("event_date") or "",
            event.get("event_type") or "",
            event.get("event_id") or "",
        )
    )

    for event in events:
        event["trading_day_context"] = _trading_day_context(event.get("event_date"))
    by_date = _attach_overlap(events)

    windows = {"next_7d": [], "next_30d": [], "next_90d": [], "later": []}
    for event in events:
        windows[upcoming_events._window_name(event["days_until_event"])].append(event)
    calendar.update(windows)
    calendar["nearest"] = events[0] if events else None

    high = [event for event in events if event.get("importance") == "HIGH"]
    verified = sum(
        1
        for event in events
        if (event.get("trading_day_context") or {}).get("verification_status") == "VERIFIED"
    )
    overlap_dates = [items for items in by_date.values() if len(items) > 1]
    high_overlap_dates = [
        items
        for items in overlap_dates
        if any(event.get("importance") == "HIGH" for event in items)
    ]
    summary = calendar.setdefault("calendar_summary", {})
    summary.update(
        {
            "event_count": len(events),
            "next_7d_event_count": len(windows["next_7d"]),
            "next_30d_event_count": sum(1 for event in events if event["days_until_event"] <= 30),
            "next_30d_high_importance_count": sum(
                1
                for event in events
                if event["days_until_event"] <= 30 and event.get("importance") == "HIGH"
            ),
            "nearest_high_importance_event_days": high[0]["days_until_event"] if high else None,
            "title_scoped_confirmed_event_count": len(title_candidates),
            "verified_trading_day_context_count": verified,
            "unverified_trading_day_context_count": len(events) - verified,
            "same_day_overlap_date_count": len(overlap_dates),
            "high_importance_overlap_date_count": len(high_overlap_dates),
        }
    )
    metadata = calendar.setdefault("metadata", {})
    metadata.update(
        {
            "company_title_date_semantics": [
                "dividend labeled record/ex/payment dates",
                "labeled resumption dates",
                "explicit shareholder-meeting dates in title",
                "labeled earnings disclosure dates",
            ],
            "dedupe_policy": (
                "logical aliases: source_event_id OR same provider+source_document_id OR exact canonical title; "
                "event_type and event_date must also match"
            ),
            "trading_day_policy": (
                "authoritative market calendar only; outside coverage/calendar errors remain UNVERIFIED"
            ),
            "overlap_policy": "same exact event_date; no trading interpretation",
        }
    )
    provenance = calendar.setdefault("provenance", {})
    layers = list(provenance.get("source_layers") or [])
    if "config/a_share_trading_calendar.json" not in layers:
        layers.append("config/a_share_trading_calendar.json")
    provenance["source_layers"] = layers
    return calendar


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    as_of = upcoming_events._as_of_date(snapshot)
    title_count = 0
    unverified_count = 0
    for stock in (snapshot.get("detail_stocks") or {}).values():
        calendar = augment_stock(stock, as_of)
        if not calendar:
            continue
        summary = calendar.get("calendar_summary") or {}
        title_count += int(summary.get("title_scoped_confirmed_event_count") or 0)
        unverified_count += int(summary.get("unverified_trading_day_context_count") or 0)

    summary = snapshot.setdefault("upcoming_events_summary", {})
    implemented = list(summary.get("implemented_sources") or [])
    for source in (
        "official_company_title_scoped_confirmed_dates",
        "authoritative_a_share_trading_calendar",
    ):
        if source not in implemented:
            implemented.append(source)
    summary["implemented_sources"] = implemented
    summary["dedupe_policy"] = "SOURCE_OR_DOCUMENT_OR_EXACT_TITLE_LOGICAL_ALIAS_WITH_TYPE_AND_DATE"
    summary["title_scoped_confirmed_event_count"] = title_count
    summary["unverified_trading_day_context_count"] = unverified_count
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "UPCOMING_EVENTS_CALENDAR "
        f"title_scoped_confirmed={title_count} unverified_trading_context={unverified_count}",
        flush=True,
    )
