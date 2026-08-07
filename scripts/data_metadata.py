import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


CST = timezone(timedelta(hours=8))
QUALITY_SEVERITY = {"PASS": 0, "DEGRADED": 1, "PARTIAL": 2, "FAILED": 3}


def _iso_cst_from_runner(snapshot):
    utc_value = snapshot.get("runner_time_utc")
    if utc_value:
        try:
            dt = datetime.fromisoformat(str(utc_value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(CST).isoformat(timespec="seconds")
        except ValueError:
            pass
    value = snapshot.get("runner_time_cst")
    if value:
        try:
            dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            pass
    return datetime.now(CST).isoformat(timespec="seconds")


def _market_time_iso(value):
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=CST).isoformat(timespec="seconds")
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST).isoformat(timespec="seconds")
    except ValueError:
        return None


def _minute_data_time(minutes):
    if not isinstance(minutes, dict):
        return None
    date = str(minutes.get("date") or "")
    tm = str(minutes.get("last_time") or "")
    if len(date) == 8 and date.isdigit() and tm:
        digits = "".join(ch for ch in tm if ch.isdigit())
        if len(digits) >= 4:
            try:
                dt = datetime.strptime(date + digits[:4], "%Y%m%d%H%M").replace(tzinfo=CST)
                return dt.isoformat(timespec="seconds")
            except ValueError:
                pass
    return None


def _source_info(source, source_type=None, source_tier=None):
    source_text = str(source or "UNKNOWN")
    lower = source_text.lower()
    if source_tier:
        tier = source_tier
    elif "history" in lower or "cache" in lower or "market-data" in lower:
        tier = "CACHE"
    elif "eastmoney" in lower:
        tier = "PRIMARY_PROVIDER"
    elif "tencent" in lower:
        tier = "SECONDARY_PROVIDER"
    elif source_text in {"DERIVED", "LOCAL_DERIVED"}:
        tier = "DERIVED"
    elif source_text in {"CNINFO", "SSE", "SZSE", "BSE", "OFFICIAL"}:
        tier = "OFFICIAL"
    else:
        tier = "UNKNOWN"

    if source_type:
        kind = source_type
    elif tier == "CACHE":
        kind = "CACHE"
    elif tier == "DERIVED":
        kind = "DERIVED"
    elif tier in {"PRIMARY_PROVIDER", "SECONDARY_PROVIDER", "OFFICIAL"}:
        kind = "API"
    else:
        kind = "UNKNOWN"
    return source_text, kind, tier


def _confidence_for_quality(quality):
    return {
        "PASS": "HIGH",
        "DEGRADED": "MEDIUM",
        "PARTIAL": "LOW",
        "FAILED": "NONE",
    }.get(quality, "LOW")


def _metadata(
    source,
    fetched_at,
    data_time=None,
    lag_seconds=None,
    freshness="UNKNOWN",
    freshness_policy="UNKNOWN",
    quality="PASS",
    fallback_used=False,
    quality_flags=None,
    source_type=None,
    source_tier=None,
    confidence=None,
):
    source, source_type, source_tier = _source_info(source, source_type, source_tier)
    return {
        "source": source,
        "source_type": source_type,
        "source_tier": source_tier,
        "fetched_at": fetched_at,
        "data_time": data_time,
        "lag_seconds": lag_seconds,
        "freshness": freshness or "UNKNOWN",
        "freshness_policy": freshness_policy,
        "confidence": confidence or _confidence_for_quality(quality),
        "quality": quality,
        "fallback_used": bool(fallback_used),
        "quality_flags": list(dict.fromkeys(quality_flags or [])),
    }


def _quality_from_status(status):
    status = str(status or "").upper()
    if status in {"OK", "PASS"}:
        return "PASS"
    if status in {"DEGRADED", "WARNING"}:
        return "DEGRADED"
    if status in {"PARTIAL", "NO_MINUTE_DATA", "NO_CURRENT_PRICE", "UNKNOWN"}:
        return "PARTIAL"
    if status in {"ERROR", "FAILED", "VIOLATION"}:
        return "FAILED"
    return "PARTIAL"


def _quote_metadata(quote, fetched_at):
    if not isinstance(quote, dict) or quote.get("latest") is None:
        return _metadata(
            "UNKNOWN",
            fetched_at,
            freshness="UNAVAILABLE",
            freshness_policy="REALTIME_QUOTE",
            quality="FAILED",
            quality_flags=["NO_VALID_DATA"],
        )

    source = quote.get("source") or "UNKNOWN"
    freshness = quote.get("freshness") or "UNKNOWN"
    resilience = quote.get("resilience") or {}
    fallback_used = bool(resilience.get("fallback_used"))
    consensus = (resilience.get("consensus") or {}).get("status")
    providers = resilience.get("providers") or {}
    flags = []

    primary = providers.get("Eastmoney") or providers.get("PRIMARY") or {}
    if primary and (primary.get("status") == "ERROR" or primary.get("usable") is False):
        flags.append("PRIMARY_SOURCE_FAILED")
    if fallback_used:
        flags.append("FALLBACK_USED")
    if consensus == "DIVERGENT":
        flags.append("SOURCE_DIVERGENCE")
    if freshness == "STALE":
        flags.append("STALE_DATA")
    if freshness in {"CURRENT_SESSION", "LAST_SESSION"}:
        flags.append("NOT_LIVE_NOW")

    if freshness == "STALE":
        quality = "PARTIAL"
    elif consensus == "DIVERGENT" or fallback_used:
        quality = "DEGRADED"
    elif freshness == "UNKNOWN":
        quality = "PARTIAL"
    else:
        quality = "PASS"

    return _metadata(
        source,
        fetched_at,
        data_time=_market_time_iso(quote.get("market_time_cst")),
        lag_seconds=quote.get("lag_seconds"),
        freshness=freshness,
        freshness_policy="REALTIME_QUOTE",
        quality=quality,
        fallback_used=fallback_used,
        quality_flags=flags,
    )


def _minutes_metadata(minutes, fetched_at):
    if not isinstance(minutes, dict) or not minutes.get("count"):
        return _metadata(
            "Tencent",
            fetched_at,
            freshness="UNAVAILABLE",
            freshness_policy="MINUTE_SERIES",
            quality="FAILED",
            quality_flags=["NO_MINUTE_DATA"],
            source_tier="SECONDARY_PROVIDER",
        )
    freshness = minutes.get("freshness") or "UNKNOWN"
    flags = []
    if freshness == "STALE":
        flags.append("STALE_MINUTE_SERIES")
    quality = "PASS" if freshness == "LIVE" else "DEGRADED" if freshness == "STALE" else "PARTIAL"
    return _metadata(
        minutes.get("source") or "Tencent",
        fetched_at,
        data_time=_minute_data_time(minutes),
        freshness=freshness,
        freshness_policy="MINUTE_SERIES",
        quality=quality,
        quality_flags=flags,
    )


def _daily_metadata(context, fetched_at):
    if not isinstance(context, dict) or not context:
        return _metadata(
            "UNKNOWN",
            fetched_at,
            freshness="UNAVAILABLE",
            freshness_policy="DAILY_K_CONTEXT",
            quality="FAILED",
            quality_flags=["NO_DAILY_CONTEXT"],
        )
    status = context.get("status")
    quality = _quality_from_status(status)
    source = context.get("source") or "UNKNOWN"
    cache = context.get("cache") or {}
    flags = []
    if str(source).lower().startswith("history"):
        flags.append("HISTORY_CACHE_USED")
    if cache.get("state") == "STALE_FALLBACK":
        flags.append("STALE_CACHE_FALLBACK")
        quality = "DEGRADED"
    if context.get("errors"):
        flags.append("SOURCE_ERRORS_PRESENT")
        if quality == "PASS":
            quality = "DEGRADED"
    latest_date = context.get("latest_completed_date")
    freshness = "LATEST_COMPLETED_BAR" if latest_date else "UNKNOWN"
    return _metadata(
        source,
        fetched_at,
        data_time=str(latest_date) if latest_date else None,
        freshness=freshness,
        freshness_policy="DAILY_K_CONTEXT",
        quality=quality,
        quality_flags=flags,
    )


def _derived_metadata(fetched_at, quality="PASS", freshness="DERIVED_CURRENT", flags=None, data_time=None):
    return _metadata(
        "DERIVED",
        fetched_at,
        data_time=data_time,
        freshness=freshness,
        freshness_policy="DERIVED",
        quality=quality,
        quality_flags=flags,
        source_type="DERIVED",
        source_tier="DERIVED",
    )


def _decorate_detail(snapshot, fetched_at):
    for code, item in (snapshot.get("detail_stocks") or {}).items():
        quote = item.get("quote")
        minutes = item.get("minutes")
        quote_meta = _quote_metadata(quote, fetched_at)
        minute_meta = _minutes_metadata(minutes, fetched_at)
        if isinstance(quote, dict):
            quote["metadata"] = quote_meta
        else:
            item["quote_metadata"] = quote_meta
        if isinstance(minutes, dict):
            minutes["metadata"] = minute_meta
        else:
            item["minutes_metadata"] = minute_meta

        intraday = item.get("intraday")
        if isinstance(intraday, dict):
            intraday_quality = _quality_from_status(intraday.get("status"))
            input_qualities = {quote_meta["quality"], minute_meta["quality"]}
            flags = []
            if "FAILED" in input_qualities:
                flags.append("INPUT_DATA_MISSING")
                if intraday_quality == "PASS":
                    intraday_quality = "PARTIAL"
            elif "DEGRADED" in input_qualities or "PARTIAL" in input_qualities:
                flags.append("INPUT_DATA_DEGRADED")
                if intraday_quality == "PASS":
                    intraday_quality = "DEGRADED"
            intraday["metadata"] = _derived_metadata(
                fetched_at,
                quality=intraday_quality,
                data_time=minute_meta.get("data_time") or quote_meta.get("data_time"),
                flags=flags,
            )
            intraday["provenance"] = {
                "type": "DERIVED",
                "derived_from": [
                    f"detail_stocks.{code}.quote",
                    f"detail_stocks.{code}.minutes",
                ],
                "algorithm": "intraday_structure_metrics_v1",
            }

        daily = item.get("daily_context")
        if isinstance(daily, dict):
            daily["metadata"] = _daily_metadata(daily, fetched_at)
            daily["provenance"] = {
                "type": "DERIVED",
                "derived_from": [f"detail_stocks.{code}.daily_context.bars_last_60"],
                "algorithm": "daily_k_context_v1",
                "field_provenance": {
                    "moving_averages.ma5": {"algorithm": "SMA", "period": 5, "derived_from": ["daily_k.close"]},
                    "moving_averages.ma10": {"algorithm": "SMA", "period": 10, "derived_from": ["daily_k.close"]},
                    "moving_averages.ma20": {"algorithm": "SMA", "period": 20, "derived_from": ["daily_k.close"]},
                    "moving_averages.ma60": {"algorithm": "SMA", "period": 60, "derived_from": ["daily_k.close"]},
                    "atr14": {"algorithm": "ATR", "period": 14, "derived_from": ["daily_k.high", "daily_k.low", "daily_k.close"]},
                    "key_levels": {"algorithm": "confluence_clustering_v1", "derived_from": ["daily_k", "moving_averages", "previous_day"]},
                },
            }

        item["metadata"] = _derived_metadata(
            fetched_at,
            quality=_quality_from_status(item.get("status")),
            data_time=quote_meta.get("data_time") or minute_meta.get("data_time"),
        )
        item["provenance"] = {
            "type": "COMPOSITE",
            "derived_from": [
                f"detail_stocks.{code}.quote",
                f"detail_stocks.{code}.minutes",
                f"detail_stocks.{code}.intraday",
                f"detail_stocks.{code}.daily_context",
            ],
        }


def _decorate_light_and_indices(snapshot, fetched_at):
    for code, item in (snapshot.get("light_stocks") or {}).items():
        quote = item.get("quote") if isinstance(item, dict) else None
        meta = _quote_metadata(quote, fetched_at)
        if isinstance(quote, dict):
            quote["metadata"] = meta
        if isinstance(item, dict):
            item["metadata"] = _derived_metadata(fetched_at, quality=_quality_from_status(item.get("status")), data_time=meta.get("data_time"))

    for name, item in (snapshot.get("indices") or {}).items():
        quote = item.get("quote") if isinstance(item, dict) else None
        meta = _quote_metadata(quote, fetched_at)
        if isinstance(quote, dict):
            quote["metadata"] = meta
        if isinstance(item, dict):
            item["metadata"] = _derived_metadata(fetched_at, quality=_quality_from_status(item.get("status")), data_time=meta.get("data_time"))
            item["provenance"] = {"type": "COMPOSITE", "derived_from": [f"indices.{name}.quote"]}


def _decorate_groups(snapshot, fetched_at):
    for group_id, group in (snapshot.get("groups") or {}).items():
        if not isinstance(group, dict):
            continue
        requested = int(group.get("requested_member_count") or 0)
        covered = int(group.get("covered_member_count") or 0)
        coverage = group.get("coverage_percent")
        flags = []
        quality = _quality_from_status(group.get("status"))
        if requested and covered < requested:
            flags.append("PEER_COVERAGE_INCOMPLETE")
            if quality == "PASS":
                quality = "DEGRADED"
        members = [m.get("code") for m in (group.get("members") or []) if isinstance(m, dict) and m.get("available")]
        group["metadata"] = _derived_metadata(fetched_at, quality=quality, flags=flags)
        group["provenance"] = {
            "type": "DERIVED",
            "derived_from": [f"quotes.{code}.change_percent" for code in members],
            "algorithm": "peer_group_summary_v1",
            "requested_member_count": requested,
            "covered_member_count": covered,
            "coverage_percent": coverage,
        }


def _decorate_system_nodes(snapshot, fetched_at):
    guard = snapshot.get("live_price_guard")
    if isinstance(guard, dict):
        quality = _quality_from_status(guard.get("status"))
        flags = []
        if guard.get("hard_violation_count"):
            flags.append("LIVE_PRICE_HARD_VIOLATION")
            quality = "FAILED"
        if guard.get("warning_count"):
            flags.append("LIVE_PRICE_WARNINGS")
            if quality == "PASS":
                quality = "DEGRADED"
        guard["metadata"] = _derived_metadata(fetched_at, quality=quality, flags=flags)
        guard["provenance"] = {"type": "DERIVED", "derived_from": ["detail_stocks.*.quote", "detail_stocks.*.minutes"], "algorithm": "live_price_guard_v1"}

    resilience = snapshot.get("quote_resilience")
    if isinstance(resilience, dict):
        quality = _quality_from_status(resilience.get("status"))
        flags = []
        if resilience.get("fallback_count"):
            flags.append("FALLBACKS_PRESENT")
        if resilience.get("divergent_count"):
            flags.append("SOURCE_DIVERGENCE_PRESENT")
        if resilience.get("unavailable_count"):
            flags.append("UNAVAILABLE_QUOTES_PRESENT")
        resilience["metadata"] = _derived_metadata(fetched_at, quality=quality, flags=flags)
        resilience["provenance"] = {"type": "DERIVED", "derived_from": ["detail_stocks.*.quote.resilience", "indices.*.quote.resilience", "light_stocks.*.quote.resilience"], "algorithm": "quote_resilience_summary_v1"}

    history = snapshot.get("history")
    if isinstance(history, dict):
        manifest = history.get("manifest") or {}
        history["metadata"] = _metadata(
            "market-data branch",
            fetched_at,
            data_time=_market_time_iso(manifest.get("latest_runner_time_cst")),
            freshness="HISTORICAL",
            freshness_policy="CACHE_HISTORY",
            quality="PASS",
            source_type="CACHE",
            source_tier="CACHE",
            quality_flags=["NOT_A_REALTIME_SOURCE"],
        )

    market_env = snapshot.get("market_environment")
    if isinstance(market_env, dict):
        quality = _quality_from_status(market_env.get("status"))
        flags = []
        if market_env.get("confidence") == "LOW":
            flags.append("LOW_CONTEXT_CONFIDENCE")
            if quality == "PASS":
                quality = "DEGRADED"
        breadth = market_env.get("breadth") or {}
        if breadth.get("estimated"):
            flags.append("BREADTH_ESTIMATED")
        if breadth.get("status") == "ERROR":
            flags.append("BREADTH_UNAVAILABLE")
        market_env["metadata"] = _derived_metadata(
            fetched_at,
            quality=quality,
            freshness=(breadth.get("freshness") or "DERIVED_CURRENT"),
            flags=flags,
            data_time=(breadth.get("market_session_date") or None),
        )
        market_env["provenance"] = {
            "type": "DERIVED",
            "derived_from": ["indices", "groups", "detail_stocks", "market_environment.breadth"],
            "algorithm": "market_environment_v1",
        }


def _iter_metadata(node, path=""):
    if isinstance(node, dict):
        meta = node.get("metadata")
        if isinstance(meta, dict):
            yield path or "$", meta
        for key, value in node.items():
            if key in {"metadata", "provenance", "data_quality", "llm_data_summary"}:
                continue
            child = f"{path}.{key}" if path else str(key)
            yield from _iter_metadata(value, child)
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            yield from _iter_metadata(value, f"{path}[{idx}]")


def _quality_summary(snapshot):
    qualities = Counter()
    tiers = Counter()
    sources = Counter()
    freshness = Counter()
    warnings = []
    critical = []

    for path, meta in _iter_metadata(snapshot):
        q = meta.get("quality") or "PARTIAL"
        qualities[q] += 1
        tiers[meta.get("source_tier") or "UNKNOWN"] += 1
        sources[meta.get("source") or "UNKNOWN"] += 1
        freshness[meta.get("freshness") or "UNKNOWN"] += 1
        if q in {"DEGRADED", "PARTIAL"}:
            warnings.append({"path": path, "quality": q, "flags": meta.get("quality_flags") or []})

    for code, item in (snapshot.get("detail_stocks") or {}).items():
        quote = item.get("quote") or {}
        meta = quote.get("metadata") or item.get("quote_metadata") or {}
        if meta.get("quality") == "FAILED":
            critical.append({"path": f"detail_stocks.{code}.quote", "reason": "NO_VALID_REALTIME_QUOTE"})

    guard = snapshot.get("live_price_guard") or {}
    if (guard.get("metadata") or {}).get("quality") == "FAILED":
        critical.append({"path": "live_price_guard", "reason": "LIVE_PRICE_GUARD_FAILED"})

    if critical:
        overall = "FAILED"
    elif qualities.get("PARTIAL"):
        overall = "PARTIAL"
    elif qualities.get("DEGRADED"):
        overall = "DEGRADED"
    else:
        overall = "PASS"

    return {
        "schema_version": 1,
        "metadata_contract": "provenance_freshness_quality_v1",
        "overall": overall,
        "critical_failures": critical,
        "warnings": warnings[:50],
        "quality_summary": dict(sorted(qualities.items())),
        "source_summary": {
            "source_tiers": dict(sorted(tiers.items())),
            "sources": dict(sorted(sources.items())),
        },
        "freshness_summary": dict(sorted(freshness.items())),
    }


def _llm_summary(snapshot, quality):
    detail = snapshot.get("detail_stocks") or {}
    quote_qualities = []
    for item in detail.values():
        quote = item.get("quote") or {}
        meta = quote.get("metadata") or item.get("quote_metadata") or {}
        quote_qualities.append(meta.get("quality") or "FAILED")

    if not quote_qualities or "FAILED" in quote_qualities:
        realtime_quality = "LOW"
    elif "PARTIAL" in quote_qualities or "DEGRADED" in quote_qualities:
        realtime_quality = "MEDIUM"
    else:
        realtime_quality = "HIGH"

    market_meta = ((snapshot.get("market_environment") or {}).get("metadata") or {})
    market_quality = {
        "PASS": "HIGH",
        "DEGRADED": "MEDIUM",
        "PARTIAL": "MEDIUM",
        "FAILED": "LOW",
    }.get(market_meta.get("quality"), "LOW")

    daily_qualities = []
    for item in detail.values():
        daily_qualities.append((((item.get("daily_context") or {}).get("metadata") or {}).get("quality") or "FAILED"))
    if daily_qualities and all(q == "PASS" for q in daily_qualities):
        history_quality = "HIGH"
    elif daily_qualities and all(q != "FAILED" for q in daily_qualities):
        history_quality = "MEDIUM"
    else:
        history_quality = "LOW"

    warning_text = []
    for warning in quality.get("warnings", [])[:12]:
        flags = warning.get("flags") or []
        suffix = ":" + ",".join(flags) if flags else ""
        warning_text.append(f"{warning.get('path')}={warning.get('quality')}{suffix}")

    return {
        "critical_data_ready": not bool(quality.get("critical_failures")),
        "realtime_quote_quality": realtime_quality,
        "market_context_quality": market_quality,
        "historical_context_quality": history_quality,
        "overall_data_quality": quality.get("overall"),
        "warnings": warning_text,
    }


def decorate_snapshot(snapshot):
    fetched_at = _iso_cst_from_runner(snapshot)
    _decorate_detail(snapshot, fetched_at)
    _decorate_light_and_indices(snapshot, fetched_at)
    _decorate_groups(snapshot, fetched_at)
    _decorate_system_nodes(snapshot, fetched_at)

    snapshot["schema_version"] = max(int(snapshot.get("schema_version") or 0), 10)
    snapshot.setdefault("features", {})["data_provenance"] = "v1"
    quality = _quality_summary(snapshot)
    snapshot["data_quality"] = quality
    snapshot["llm_data_summary"] = _llm_summary(snapshot, quality)
    return snapshot


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    decorate_snapshot(data)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "DATA_METADATA "
        f"overall={data['data_quality']['overall']} "
        f"critical={len(data['data_quality']['critical_failures'])} "
        f"warnings={len(data['data_quality']['warnings'])} "
        f"critical_ready={data['llm_data_summary']['critical_data_ready']}",
        flush=True,
    )
    print("SNAPSHOT_SCHEMA_UPGRADED schema_version=10 feature=data_provenance:v1", flush=True)
