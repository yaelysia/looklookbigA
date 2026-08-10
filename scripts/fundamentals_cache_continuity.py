def _report_date(row):
    if not isinstance(row, dict):
        return None
    value = row.get("REPORT_DATE") or row.get("REPORTDATE")
    text = str(value or "")
    return text[:10] if len(text) >= 10 else None


def _rows_by_date(rows):
    out = {}
    for row in rows or []:
        date = _report_date(row)
        if not date:
            continue
        current = out.get(date)
        if current is None:
            out[date] = dict(row)
            continue
        new_type = str(row.get("REPORT_TYPE") or row.get("REPORTTYPE") or row.get("DATATYPE") or "").upper()
        old_type = str(current.get("REPORT_TYPE") or current.get("REPORTTYPE") or current.get("DATATYPE") or "").upper()
        if "合并" in new_type and "合并" not in old_type:
            out[date] = dict(row)
    return out


def _merge_row(old, fresh):
    value = dict(old or {})
    for key, item in (fresh or {}).items():
        if item not in (None, "", "-") or key not in value:
            value[key] = item
    return value


def _merge_report_class(fresh_rows, cached_rows):
    fresh = _rows_by_date(fresh_rows)
    cached = _rows_by_date(cached_rows)
    fresh_latest = max(fresh) if fresh else None
    cached_latest = max(cached) if cached else None
    if fresh_latest and cached_latest and fresh_latest < cached_latest:
        return [cached[key] for key in sorted(cached, reverse=True)], {
            "status": "REGRESSED",
            "provider_latest": fresh_latest,
            "cache_latest": cached_latest,
        }
    merged = dict(cached)
    for date, row in fresh.items():
        merged[date] = _merge_row(cached.get(date), row)
    return [merged[key] for key in sorted(merged, reverse=True)], {
        "status": "MERGED" if cached else "FRESH",
        "provider_latest": fresh_latest,
        "cache_latest": cached_latest,
    }


def install(fundamentals_context):
    if getattr(fundamentals_context, "_report_cache_continuity_installed", False):
        return
    original_fetch = fundamentals_context.fetch_all_reports
    original_write = fundamentals_context._write_json
    state = {}

    def fetch_all_reports(base, code):
        fresh, fresh_urls, errors = original_fetch(base, code)
        cache = fundamentals_context._load_json(fundamentals_context._cache_path(code)) or {}
        cached_raw = cache.get("raw") if isinstance(cache.get("raw"), dict) else {}
        cached_urls = cache.get("source_urls") if isinstance(cache.get("source_urls"), dict) else {}
        merged, urls = {}, {}
        fallback_keys, regressed = [], {}
        for key in fundamentals_context.REPORTS:
            rows = fresh.get(key) if isinstance(fresh.get(key), list) else []
            old = cached_raw.get(key) if isinstance(cached_raw.get(key), list) else []
            if not rows:
                merged[key] = old
                urls[key] = cached_urls.get(key) or fresh_urls.get(key)
                if old:
                    fallback_keys.append(key)
                continue
            merged_rows, continuity = _merge_report_class(rows, old)
            merged[key] = merged_rows
            if continuity["status"] == "REGRESSED":
                urls[key] = cached_urls.get(key) or fresh_urls.get(key)
                regressed[key] = continuity
            else:
                urls[key] = fresh_urls.get(key) or cached_urls.get(key)
        extra = list(errors or [])
        if fallback_keys:
            extra.append("CACHE_FALLBACK_REPORT_CLASSES=" + ",".join(sorted(fallback_keys)))
        for key, value in sorted(regressed.items()):
            extra.append(
                f"PROVIDER_REPORT_WINDOW_REGRESSED={key}:provider={value['provider_latest']}:cache={value['cache_latest']}"
            )
        state[str(code)] = {
            "fallback_report_classes": sorted(fallback_keys),
            "regressed_report_classes": regressed,
        }
        return merged, urls, extra

    def write_json(path, value):
        code = str((value or {}).get("code") or "") if isinstance(value, dict) else ""
        continuity = state.pop(code, None) if code else None
        if continuity and isinstance(value, dict) and isinstance(value.get("raw"), dict):
            fallback = continuity.get("fallback_report_classes") or []
            regressed = continuity.get("regressed_report_classes") or {}
            if fallback or regressed:
                old = fundamentals_context._load_json(path) or {}
                attempted = value.get("fetched_at")
                if old.get("fetched_at"):
                    value["fetched_at"] = old.get("fetched_at")
                value["last_refresh_attempt_at"] = attempted
                value["continuity"] = continuity
        original_write(path, value)

    fundamentals_context.fetch_all_reports = fetch_all_reports
    fundamentals_context._write_json = write_json
    fundamentals_context._report_cache_continuity_installed = True
