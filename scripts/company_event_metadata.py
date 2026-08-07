import json
from pathlib import Path

import data_metadata


def _quality(status):
    status = str(status or "").upper()
    if status == "OK":
        return "PASS"
    if status == "DEGRADED":
        return "DEGRADED"
    if status == "PARTIAL":
        return "PARTIAL"
    return "FAILED"


def _event_metadata(event, fetched_at):
    freshness = event.get("freshness") or "UNKNOWN"
    quality = "PASS" if event.get("source_tier") == "OFFICIAL" and event.get("source_url") else "PARTIAL"
    flags = []
    if not event.get("source_url"):
        flags.append("SOURCE_URL_MISSING")
    if not event.get("published_at"):
        flags.append("PUBLISHED_TIME_MISSING")
        quality = "PARTIAL"
    if event.get("supersedes_event_id"):
        flags.append("CORRECTION_OR_SUPERSEDING_EVENT")
    return data_metadata._metadata(
        event.get("source") or "CNINFO",
        fetched_at,
        data_time=event.get("published_at"),
        freshness=freshness,
        freshness_policy="OFFICIAL_DISCLOSURE",
        quality=quality,
        quality_flags=flags,
        source_type="API",
        source_tier=event.get("source_tier") or "OFFICIAL",
    )


def _container_metadata(events, fetched_at):
    status = events.get("status")
    quality = _quality(status)
    flags = []
    cache = events.get("cache") or {}
    if status == "DEGRADED":
        flags.extend(["PROVIDER_FAILED", "EVENT_CACHE_FALLBACK"])
    elif status == "PARTIAL":
        flags.append("EVENT_QUERY_PARTIAL")
    elif status == "ERROR":
        flags.append("EVENT_PROVIDER_FAILED_NO_USABLE_CACHE")
    if cache.get("state") == "STALE_FALLBACK":
        flags.append("STALE_EVENT_CACHE_USED")
    return data_metadata._metadata(
        events.get("source") or "CNINFO",
        fetched_at,
        data_time=((events.get("latest") or {}).get("published_at")),
        freshness="CURRENT_QUERY" if status == "OK" else "DEGRADED_QUERY" if status in {"PARTIAL", "DEGRADED"} else "UNAVAILABLE",
        freshness_policy="OFFICIAL_DISCLOSURE_SET",
        quality=quality,
        fallback_used=cache.get("state") == "STALE_FALLBACK",
        quality_flags=flags,
        source_type="API",
        source_tier=events.get("source_tier") or "OFFICIAL",
    )


def _decorate_event(event, fetched_at):
    event["metadata"] = _event_metadata(event, fetched_at)
    event["provenance"] = {
        "type": "OFFICIAL_DISCLOSURE",
        "provider": event.get("source"),
        "document_id": event.get("source_document_id"),
        "document_url": event.get("source_url"),
        "fact_extraction": (event.get("facts") or {}).get("extraction_scope"),
        "classification": "deterministic_title_rules_v1",
    }


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    fetched_at = data_metadata._iso_cst_from_runner(data)

    for code, item in (data.get("detail_stocks") or {}).items():
        events = item.get("events")
        if not isinstance(events, dict):
            continue
        events["metadata"] = _container_metadata(events, fetched_at)
        events["provenance"] = {
            "type": "OFFICIAL_DISCLOSURE_SET",
            "provider": "CNINFO",
            "source_tier": "OFFICIAL",
            "cache_path": (events.get("cache") or {}).get("path"),
            "lookback_days": events.get("lookback_days"),
        }
        # latest/recent/upcoming are separate objects after snapshot JSON is
        # reloaded between finalizers. Decorate every representation so an LLM
        # sees the same metadata regardless of which view it reads.
        for bucket in ("latest", "recent", "upcoming"):
            value = events.get(bucket)
            values = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
            for event in values:
                if isinstance(event, dict) and event.get("event_id"):
                    _decorate_event(event, fetched_at)

    summary = data.get("company_events")
    if isinstance(summary, dict):
        status = summary.get("status")
        summary["metadata"] = data_metadata._metadata(
            "CNINFO",
            fetched_at,
            freshness="DERIVED_CURRENT",
            freshness_policy="EVENT_SUMMARY",
            quality=_quality(status),
            quality_flags=["SOME_DETAIL_EVENT_SOURCES_DEGRADED"] if status == "PARTIAL" else [],
            source_type="DERIVED",
            source_tier="DERIVED",
        )
        summary["provenance"] = {
            "type": "DERIVED",
            "derived_from": ["detail_stocks.*.events"],
            "algorithm": "company_events_summary_v1",
        }

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
