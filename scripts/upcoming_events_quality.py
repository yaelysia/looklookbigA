import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


CST = timezone(timedelta(hours=8))
QUALITY_SEVERITY = {"PASS": 0, "DEGRADED": 1, "PARTIAL": 2, "FAILED": 3}
CONFIDENCE = {"PASS": "HIGH", "DEGRADED": "MEDIUM", "PARTIAL": "LOW", "FAILED": "NONE"}
GOOD_SOURCE_STATUSES = {"OK", "PASS"}


def _runner_time_iso(snapshot):
    utc_value = snapshot.get("runner_time_utc")
    if utc_value:
        try:
            value = datetime.fromisoformat(str(utc_value).replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(CST).isoformat(timespec="seconds")
        except ValueError:
            pass
    cst_value = snapshot.get("runner_time_cst")
    if cst_value:
        text = str(cst_value).strip()
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=CST)
            return value.astimezone(CST).isoformat(timespec="seconds")
        except ValueError:
            try:
                value = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
                return value.isoformat(timespec="seconds")
            except ValueError:
                pass
    return datetime.now(CST).isoformat(timespec="seconds")


def _quality_from_status(status):
    value = str(status or "").upper()
    if value in {"OK", "PASS"}:
        return "PASS"
    if value in {"DEGRADED", "WARNING"}:
        return "DEGRADED"
    if value in {"PARTIAL", "UNKNOWN", "DEFERRED"}:
        return "PARTIAL"
    if value in {"UNAVAILABLE", "FAILED", "ERROR"}:
        return "FAILED"
    return "PARTIAL"


def _worse(left, right):
    left = left if left in QUALITY_SEVERITY else "PARTIAL"
    right = right if right in QUALITY_SEVERITY else "PARTIAL"
    return left if QUALITY_SEVERITY[left] >= QUALITY_SEVERITY[right] else right


def _source_status_flags(source_status):
    flags = []
    if not isinstance(source_status, dict):
        return flags
    for name, status in sorted(source_status.items()):
        normalized = str(status or "UNKNOWN").upper()
        if normalized not in GOOD_SOURCE_STATUSES:
            safe = "".join(ch if ch.isalnum() else "_" for ch in str(name).upper()).strip("_")
            flags.append(f"SOURCE_{safe}_{normalized}")
    return flags


def _decorate_layer(layer, fetched_at):
    metadata = layer.setdefault("metadata", {})
    quality = _quality_from_status(layer.get("status"))
    existing_quality = metadata.get("quality")
    if existing_quality:
        quality = _worse(quality, str(existing_quality).upper())

    flags = list(metadata.get("quality_flags") or [])
    flags.extend(_source_status_flags(metadata.get("source_status")))

    excluded = int(metadata.get("excluded_unproven_company_event_count") or 0)
    if excluded:
        flags.append("UNPROVEN_DATES_EXCLUDED_FAIL_CLOSED")

    summary = layer.get("calendar_summary") or {}
    unverified = int(summary.get("unverified_trading_day_context_count") or 0)
    if unverified:
        flags.append("TRADING_DAY_CONTEXT_UNVERIFIED")
        if quality == "PASS":
            quality = "DEGRADED"

    metadata.update(
        {
            "source": "DERIVED",
            "source_type": "DERIVED",
            "source_tier": "DERIVED",
            "fetched_at": fetched_at,
            "data_time": layer.get("as_of_date"),
            "lag_seconds": None,
            "freshness": metadata.get("freshness") or "DERIVED_FROM_CURRENT_SNAPSHOT_FACTS",
            "freshness_policy": "UPCOMING_EVENTS_CURRENT_SNAPSHOT_FACTS",
            "confidence": CONFIDENCE[quality],
            "quality": quality,
            "fallback_used": False,
            "quality_flags": list(dict.fromkeys(flags)),
        }
    )

    provenance = layer.setdefault("provenance", {})
    source_layers = list(provenance.get("source_layers") or [])
    provenance.update(
        {
            "type": "DERIVED",
            "derived_from": source_layers,
            "algorithm": "upcoming_events_v1",
            "date_policy": "TYPE_SCOPED_EXPLICIT_DATES_ONLY",
            "quality_contract": "provenance_freshness_quality_v1",
        }
    )
    return quality


def _summary_quality(qualities):
    values = list(qualities)
    if not values:
        return "FAILED"
    if "FAILED" in values or "PARTIAL" in values:
        return "PARTIAL"
    if "DEGRADED" in values:
        return "DEGRADED"
    return "PASS"


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    fetched_at = _runner_time_iso(snapshot)
    qualities = {}
    flags = Counter()

    for code, stock in (snapshot.get("detail_stocks") or {}).items():
        layer = (stock or {}).get("upcoming_events")
        if not isinstance(layer, dict):
            continue
        quality = _decorate_layer(layer, fetched_at)
        qualities[code] = quality
        for flag in (layer.get("metadata") or {}).get("quality_flags") or []:
            flags[flag] += 1

    summary = snapshot.setdefault("upcoming_events_summary", {})
    overall_quality = _summary_quality(qualities.values())
    summary["quality_by_code"] = dict(sorted(qualities.items()))
    summary["quality_flag_counts"] = dict(sorted(flags.items()))
    summary["metadata"] = {
        "source": "DERIVED",
        "source_type": "DERIVED",
        "source_tier": "DERIVED",
        "fetched_at": fetched_at,
        "data_time": None,
        "lag_seconds": None,
        "freshness": "DERIVED_FROM_CURRENT_SNAPSHOT_FACTS",
        "freshness_policy": "UPCOMING_EVENTS_CURRENT_SNAPSHOT_FACTS",
        "confidence": CONFIDENCE[overall_quality],
        "quality": overall_quality,
        "fallback_used": False,
        "quality_flags": [flag for flag, count in sorted(flags.items()) if count > 0],
    }
    summary["provenance"] = {
        "type": "COMPOSITE",
        "derived_from": ["detail_stocks.*.upcoming_events"],
        "algorithm": "upcoming_events_summary_v1",
        "quality_contract": "provenance_freshness_quality_v1",
    }
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "UPCOMING_EVENTS_QUALITY "
        f"quality={overall_quality} detail_stocks={len(qualities)} flags={sum(flags.values())}",
        flush=True,
    )
