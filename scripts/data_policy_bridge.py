import json
from collections import Counter
from pathlib import Path

import data_policy


VALIDATED_DAILY_CACHE_STATES = {
    "HIT",
    "BOOTSTRAP",
    "INCREMENTAL_REFRESH",
    "FULL_REFRESH",
}


def install(data_metadata):
    if getattr(data_metadata, "_data_policy_bridge_installed", False):
        return

    original_metadata = data_metadata._metadata
    original_minutes_metadata = data_metadata._minutes_metadata
    original_daily_metadata = data_metadata._daily_metadata
    original_quality_summary = data_metadata._quality_summary
    original_llm_summary = data_metadata._llm_summary
    original_decorate_snapshot = data_metadata.decorate_snapshot

    def metadata(*args, **kwargs):
        first_seen_at = kwargs.pop("first_seen_at", None)
        explicit_data_class = kwargs.pop("data_class", None)
        session_verified = kwargs.pop("session_verified", None)
        completed_session_age = kwargs.pop("completed_session_age", None)
        session_validation_state = kwargs.pop("session_validation_state", None)
        value = original_metadata(*args, **kwargs)
        data_class = data_policy.data_class_for_metadata(
            value.get("freshness_policy"),
            explicit=explicit_data_class,
        )
        value["trust"] = data_policy.source_trust(
            value.get("source"),
            source_type=value.get("source_type"),
            source_tier=value.get("source_tier"),
        )
        value["freshness_sla"] = data_policy.evaluate_freshness_sla(
            data_class,
            freshness=value.get("freshness"),
            lag_seconds=value.get("lag_seconds"),
            data_time=value.get("data_time"),
            fetched_at=value.get("fetched_at"),
            first_seen_at=first_seen_at,
            session_verified=session_verified,
            completed_session_age=completed_session_age,
            session_validation_state=session_validation_state,
        )
        return value

    def minutes_metadata(minutes, fetched_at):
        value = original_minutes_metadata(minutes, fetched_at)
        if isinstance(minutes, dict):
            # live_price_guard owns the minute timestamp/lag calculation. Carry
            # that exact observed lag into the unified metadata contract instead
            # of recomputing or silently dropping it.
            value["lag_seconds"] = minutes.get("lag_seconds")
        value["freshness_sla"] = data_policy.evaluate_freshness_sla(
            "MINUTE_SERIES",
            freshness=value.get("freshness"),
            lag_seconds=value.get("lag_seconds"),
            data_time=value.get("data_time"),
            fetched_at=value.get("fetched_at"),
        )
        return value

    def _daily_session_evidence(context, fetched_at):
        context = context if isinstance(context, dict) else {}
        cache = context.get("cache") or {}
        state = str(cache.get("state") or "UNKNOWN").upper()
        validation_key = str(cache.get("validation_key") or "")
        fetched = data_policy._parse_dt(fetched_at)
        validation_date = validation_key.split(":", 1)[0] if ":" in validation_key else None
        validation_current = bool(
            fetched
            and validation_date
            and validation_date == fetched.date().isoformat()
        )

        latest_completed = str(context.get("latest_completed_date") or "")
        previous_day = context.get("previous_day") or {}
        previous_date = str(previous_day.get("date") or "")
        anchor_consistent = bool(
            latest_completed
            and previous_date
            and latest_completed == previous_date
        )
        verified = bool(
            state in VALIDATED_DAILY_CACHE_STATES
            and validation_current
            and anchor_consistent
        )

        # Age is expressed in observed trading sessions, not calendar days.
        # During an active session the current partial bar makes the latest
        # completed bar one session old; otherwise the latest validated
        # completed bar is age zero. We do not fabricate an age if validation
        # failed or is stale.
        current_partial = context.get("current_partial_bar")
        completed_session_age = (
            1
            if verified and isinstance(current_partial, dict) and current_partial.get("date")
            else 0 if verified else None
        )
        return {
            "verified": verified,
            "completed_session_age": completed_session_age,
            "cache_state": state,
            "validation_key": validation_key or None,
            "validation_current": validation_current,
            "anchor_consistent": anchor_consistent,
        }

    def daily_metadata(context, fetched_at):
        value = original_daily_metadata(context, fetched_at)
        evidence = _daily_session_evidence(context, fetched_at)
        if value.get("data_time"):
            value["freshness"] = (
                "LATEST_COMPLETED_BAR"
                if evidence["verified"]
                else "COMPLETED_BAR_UNVERIFIED"
            )
        value["freshness_sla"] = data_policy.evaluate_freshness_sla(
            "DAILY_K",
            freshness=value.get("freshness"),
            data_time=value.get("data_time"),
            fetched_at=value.get("fetched_at"),
            session_verified=evidence["verified"],
            completed_session_age=evidence["completed_session_age"],
            session_validation_state=evidence["cache_state"],
        )
        value["freshness_sla"]["session_evidence"] = {
            "cache_state": evidence["cache_state"],
            "validation_key": evidence["validation_key"],
            "validation_current": evidence["validation_current"],
            "anchor_consistent": evidence["anchor_consistent"],
        }
        return value

    def quality_summary(snapshot):
        summary = original_quality_summary(snapshot)
        trust_tiers = Counter()
        trust_classes = Counter()
        sla_statuses = Counter()
        sla_violations = []
        sla_unmeasured = []

        for path, meta in data_metadata._iter_metadata(snapshot):
            trust = meta.get("trust") or {}
            sla = meta.get("freshness_sla") or {}
            trust_tiers[trust.get("tier") or "UNKNOWN"] += 1
            trust_classes[trust.get("class") or "UNKNOWN"] += 1
            status = sla.get("status") or "UNMEASURED"
            sla_statuses[status] += 1
            if status == "VIOLATED":
                sla_violations.append({
                    "path": path,
                    "data_class": sla.get("data_class"),
                    "measurement": sla.get("measurement"),
                    "observed_lag_seconds": sla.get("observed_lag_seconds"),
                    "observed_discovery_lag_seconds": sla.get("observed_discovery_lag_seconds"),
                    "observed_completed_session_age": sla.get("observed_completed_session_age"),
                    "reason": sla.get("reason"),
                })
            elif status == "UNMEASURED":
                sla_unmeasured.append({
                    "path": path,
                    "data_class": sla.get("data_class"),
                    "reason": sla.get("reason"),
                })

        summary["policy_versions"] = {
            "source_trust_model": data_policy.SOURCE_TRUST_MODEL_VERSION,
            "freshness_sla": data_policy.FRESHNESS_SLA_VERSION,
        }
        summary.setdefault("source_summary", {})["trust_tiers"] = dict(sorted(trust_tiers.items()))
        summary["source_summary"]["trust_classes"] = dict(sorted(trust_classes.items()))
        summary["freshness_sla_summary"] = dict(sorted(sla_statuses.items()))
        summary["freshness_sla_violation_count"] = int(sla_statuses.get("VIOLATED", 0))
        summary["freshness_sla_unmeasured_count"] = int(sla_statuses.get("UNMEASURED", 0))
        summary["freshness_sla_violations"] = sla_violations[:50]
        summary["freshness_sla_unmeasured"] = sla_unmeasured[:50]
        return summary

    def llm_summary(snapshot, quality):
        value = original_llm_summary(snapshot, quality)
        value["source_trust_model"] = data_policy.SOURCE_TRUST_MODEL_VERSION
        value["freshness_sla_model"] = data_policy.FRESHNESS_SLA_VERSION
        value["freshness_sla_violation_count"] = int(
            quality.get("freshness_sla_violation_count")
            if quality.get("freshness_sla_violation_count") is not None
            else (quality.get("freshness_sla_summary") or {}).get("VIOLATED", 0)
        )
        value["freshness_sla_unmeasured_count"] = int(
            quality.get("freshness_sla_unmeasured_count")
            if quality.get("freshness_sla_unmeasured_count") is not None
            else (quality.get("freshness_sla_summary") or {}).get("UNMEASURED", 0)
        )
        return value

    def decorate_snapshot(snapshot):
        value = original_decorate_snapshot(snapshot)
        value["schema_version"] = max(int(value.get("schema_version") or 0), 13)
        value.setdefault("features", {})["data_policy"] = "v1"
        value["data_policy"] = data_policy.policy_manifest()
        return value

    def finalize_snapshot(snapshot_path):
        path = Path(snapshot_path)
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        decorate_snapshot(snapshot)
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        quality = snapshot["data_quality"]
        print(
            "DATA_METADATA "
            f"overall={quality['overall']} "
            f"critical={len(quality['critical_failures'])} "
            f"noncritical_failed={len(quality['noncritical_failures'])} "
            f"warnings={len(quality['warnings'])} "
            f"sla_violations={quality.get('freshness_sla_violation_count', 0)} "
            f"sla_unmeasured={quality.get('freshness_sla_unmeasured_count', 0)} "
            f"critical_ready={snapshot['llm_data_summary']['critical_data_ready']}",
            flush=True,
        )
        print(
            "SNAPSHOT_SCHEMA_UPGRADED schema_version=13 feature=data_policy:v1",
            flush=True,
        )

    data_metadata._metadata = metadata
    data_metadata._minutes_metadata = minutes_metadata
    data_metadata._daily_metadata = daily_metadata
    data_metadata._quality_summary = quality_summary
    data_metadata._llm_summary = llm_summary
    data_metadata.decorate_snapshot = decorate_snapshot
    data_metadata.finalize_snapshot = finalize_snapshot
    data_metadata._data_policy_bridge_installed = True
