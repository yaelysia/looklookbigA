def install(fundamentals_context):
    if getattr(fundamentals_context, "_report_cache_continuity_installed", False):
        return
    original_fetch = fundamentals_context.fetch_all_reports

    def fetch_all_reports(base, code):
        fresh, fresh_urls, errors = original_fetch(base, code)
        cache = fundamentals_context._load_json(fundamentals_context._cache_path(code)) or {}
        cached_raw = cache.get("raw") if isinstance(cache.get("raw"), dict) else {}
        cached_urls = cache.get("source_urls") if isinstance(cache.get("source_urls"), dict) else {}
        merged = {}
        urls = {}
        fallback_keys = []
        for key in fundamentals_context.REPORTS:
            rows = fresh.get(key) if isinstance(fresh.get(key), list) else []
            if rows:
                merged[key] = rows
                urls[key] = fresh_urls.get(key)
            else:
                old = cached_raw.get(key) if isinstance(cached_raw.get(key), list) else []
                merged[key] = old
                urls[key] = fresh_urls.get(key) or cached_urls.get(key)
                if old:
                    fallback_keys.append(key)
        if fallback_keys:
            errors = list(errors or []) + [
                "CACHE_FALLBACK_REPORT_CLASSES=" + ",".join(sorted(fallback_keys))
            ]
        return merged, urls, errors

    fundamentals_context.fetch_all_reports = fetch_all_reports
    fundamentals_context._report_cache_continuity_installed = True
