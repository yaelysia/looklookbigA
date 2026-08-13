HEALTHY_SOURCE_STATUSES = {"OK", "PASS"}
UNAVAILABLE_SOURCE_STATUSES = {"UNAVAILABLE"}
FAILED_SOURCE_STATUSES = {
    "ERROR",
    "FAILED",
    "FAILURE",
    "FATAL",
    "TIMEOUT",
    "TIMED_OUT",
    "CANCELLED",
    "ABORTED",
}
DEGRADED_SOURCE_STATUSES = {"PARTIAL", "DEGRADED", "DEFERRED", "UNKNOWN", "WARNING"}


def _normalized(value):
    value = str(value or "UNAVAILABLE").strip().upper()
    return value or "UNAVAILABLE"


def aggregate(source_status):
    values = [_normalized(value) for value in (source_status or {}).values()]
    has_healthy = any(value in HEALTHY_SOURCE_STATUSES for value in values)
    has_failed = any(value in FAILED_SOURCE_STATUSES for value in values)
    has_degraded = any(
        value in DEGRADED_SOURCE_STATUSES
        or value not in HEALTHY_SOURCE_STATUSES | UNAVAILABLE_SOURCE_STATUSES | FAILED_SOURCE_STATUSES
        for value in values
    )
    if has_failed:
        return "PARTIAL" if has_healthy or has_degraded else "FAILED"
    if has_degraded:
        return "PARTIAL"
    if has_healthy:
        return "OK"
    return "UNAVAILABLE"


def install(upcoming_events_module):
    if getattr(upcoming_events_module, "_source_status_fail_closed_installed", False):
        return
    original_build = upcoming_events_module.build_upcoming_events

    def build_upcoming_events(stock, as_of):
        value = original_build(stock, as_of)
        metadata = value.setdefault("metadata", {})
        status = aggregate(metadata.get("source_status"))
        value["status"] = status
        metadata["quality"] = (
            "PASS" if status == "OK" else "PARTIAL" if status == "PARTIAL" else "FAILED"
        )
        metadata["source_failure_policy"] = (
            "terminal source failures fail closed: FAILED without a usable source; "
            "PARTIAL when mixed with usable or degraded sources"
        )
        return value

    upcoming_events_module.build_upcoming_events = build_upcoming_events
    upcoming_events_module._source_status_fail_closed_installed = True
