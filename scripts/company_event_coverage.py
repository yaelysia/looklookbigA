from datetime import datetime, timedelta


MAX_SPLIT_DEPTH = 8


def _parse_date(value):
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _dedupe_rows(rows):
    out = {}
    fallback = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        announcement_id = row.get("announcementId") or row.get("announcement_id")
        if announcement_id not in (None, ""):
            out[str(announcement_id)] = row
        else:
            fallback.append(row)
    return list(out.values()) + fallback


def _range_query(events, code, org_id, start_date, end_date, depth=0):
    start_text = start_date.isoformat()
    end_text = end_date.isoformat()
    first_rows, total = events._announcement_page(code, org_id, start_text, end_text, 1)
    pages_total = max(1, int(events.math.ceil(total / events.PAGE_SIZE))) if total else 1

    # A wide window that exceeds the fixed page cap is not allowed to be
    # silently truncated. Split by date until each leaf fits the cap.
    if pages_total > events.MAX_PAGES and start_date < end_date and depth < MAX_SPLIT_DEPTH:
        span_days = (end_date - start_date).days
        midpoint = start_date + timedelta(days=span_days // 2)
        right_start = midpoint + timedelta(days=1)
        left_rows, left_meta = _range_query(events, code, org_id, start_date, midpoint, depth + 1)
        right_rows, right_meta = _range_query(events, code, org_id, right_start, end_date, depth + 1)
        rows = _dedupe_rows(left_rows + right_rows)
        errors = list(left_meta.get("errors") or []) + list(right_meta.get("errors") or [])
        missing_ranges = list(left_meta.get("missing_ranges") or []) + list(right_meta.get("missing_ranges") or [])
        return rows, {
            "total_record_num": total,
            "pages_total": pages_total,
            "pages_requested": 1 + int(left_meta.get("pages_requested") or 0) + int(right_meta.get("pages_requested") or 0),
            "rows_received": len(rows),
            "complete": bool(left_meta.get("complete") and right_meta.get("complete")),
            "errors": errors,
            "missing_ranges": missing_ranges,
            "split_used": True,
            "segments": list(left_meta.get("segments") or []) + list(right_meta.get("segments") or []),
        }

    pages_requested = min(pages_total, events.MAX_PAGES)
    rows = list(first_rows)
    errors = []
    missing_ranges = []
    for page in range(2, pages_requested + 1):
        try:
            page_rows, _ = events._announcement_page(code, org_id, start_text, end_text, page)
            rows.extend(page_rows)
        except Exception as exc:
            errors.append(f"{start_text}~{end_text} page {page}: {type(exc).__name__}: {exc}")
            missing_ranges.append({
                "start_date": start_text,
                "end_date": end_text,
                "page": page,
                "reason": "PAGE_REQUEST_FAILED",
            })

    if pages_total > events.MAX_PAGES:
        errors.append(f"{start_text}~{end_text} query capped at {events.MAX_PAGES}/{pages_total} pages")
        missing_ranges.append({
            "start_date": start_text,
            "end_date": end_text,
            "page_start": events.MAX_PAGES + 1,
            "page_end": pages_total,
            "reason": "PAGE_CAP_EXCEEDED",
        })

    rows = _dedupe_rows(rows)
    complete = not errors and pages_total <= events.MAX_PAGES
    return rows, {
        "total_record_num": total,
        "pages_total": pages_total,
        "pages_requested": pages_requested,
        "rows_received": len(rows),
        "complete": complete,
        "errors": errors,
        "missing_ranges": missing_ranges,
        "split_used": False,
        "segments": [{
            "start_date": start_text,
            "end_date": end_text,
            "pages_total": pages_total,
            "pages_requested": pages_requested,
            "complete": complete,
        }],
    }


def install(events):
    if getattr(events, "_coverage_hardening_installed", False):
        return

    original_resolve_query_start = events._resolve_query_start
    original_cache_payload = events._cache_payload
    original_fetch_events_for_code = events.fetch_events_for_code

    events.EVENT_CACHE_SCHEMA = max(int(getattr(events, "EVENT_CACHE_SCHEMA", 1)), 2)
    events._last_coverage_query_meta = None

    def query_announcements(code, org_id, start_date, end_date):
        rows, meta = _range_query(
            events,
            code,
            org_id,
            _parse_date(start_date),
            _parse_date(end_date),
        )
        events._last_coverage_query_meta = meta
        return rows, meta

    def resolve_query_start(cache, desired_start, now):
        cache = cache or {}
        explicit_complete = cache.get("coverage_complete")
        if explicit_complete is None:
            # Legacy cache compatibility: old PARTIAL caches must be treated as
            # incomplete even if they contain a covered_start_date.
            explicit_complete = str(cache.get("query_status") or "").upper() == "OK"
        query_status = str(cache.get("query_status") or "").upper()
        if not explicit_complete or query_status not in {"", "OK"}:
            return desired_start, "BACKFILL_INCOMPLETE"
        return original_resolve_query_start(cache, desired_start, now)

    def cache_payload(code, org_id, cached_events, covered_start_date, now, query_status):
        payload = original_cache_payload(
            code,
            org_id,
            cached_events,
            covered_start_date,
            now,
            query_status,
        )
        meta = events._last_coverage_query_meta or {}
        complete = bool(query_status == "OK" and meta.get("complete"))
        payload["schema_version"] = events.EVENT_CACHE_SCHEMA
        payload["requested_start_date"] = covered_start_date.isoformat()
        payload["coverage_complete"] = complete
        payload["covered_start_date"] = covered_start_date.isoformat() if complete else None
        payload["incomplete_ranges"] = list(meta.get("missing_ranges") or [])
        payload["query_diagnostics"] = {
            "complete": bool(meta.get("complete")),
            "split_used": bool(meta.get("split_used")),
            "pages_total": meta.get("pages_total"),
            "pages_requested": meta.get("pages_requested"),
            "rows_received": meta.get("rows_received"),
            "errors": list(meta.get("errors") or []),
            "segments": list(meta.get("segments") or []),
        }
        return payload

    def fetch_events_for_code(code, lookback_days, now=None):
        events._last_coverage_query_meta = None
        result = original_fetch_events_for_code(code, lookback_days, now=now)
        cache = events._read_json(events._event_cache_path(code)) or {}
        cache_info = result.setdefault("cache", {})
        cache_info["query_status"] = cache.get("query_status")
        cache_info["coverage_complete"] = cache.get("coverage_complete")
        cache_info["covered_start_date"] = cache.get("covered_start_date")
        cache_info["requested_start_date"] = cache.get("requested_start_date")
        cache_info["incomplete_ranges"] = list(cache.get("incomplete_ranges") or [])
        return result

    events._query_announcements = query_announcements
    events._resolve_query_start = resolve_query_start
    events._cache_payload = cache_payload
    events.fetch_events_for_code = fetch_events_for_code
    events._coverage_hardening_installed = True
