import json
from collections import Counter
from pathlib import Path

import data_policy


def install(data_metadata):
    if getattr(data_metadata, "_data_policy_bridge_installed", False):
        return

    original_metadata = data_metadata._metadata
    original_quality_summary = data_metadata._quality_summary
    original_llm_summary = data_metadata._llm_summary
    original_decorate_snapshot = data_metadata.decorate_snapshot

    def metadata(*args, **kwargs):
        first_seen_at = kwargs.pop("first_seen_at", None)
        explicit_data_class = kwargs.pop("data_class", None)
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
        )
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
        summary["freshness_sla_violations"] = sla_violations[:50]
        summary["freshness_sla_unmeasured"] = sla_unmeasured[:50]
        return summary

    def llm_summary(snapshot, quality):
        value = original_llm_summary(snapshot, quality)
        value["source_trust_model"] = data_policy.SOURCE_TRUST_MODEL_VERSION
        value["freshness_sla_model"] = data_policy.FRESHNESS_SLA_VERSION
        value["freshness_sla_violation_count"] = len(quality.get("freshness_sla_violations") or [])
        value["freshness_sla_unmeasured_count"] = len(quality.get("freshness_sla_unmeasured") or [])
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
            f"sla_violations={len(quality.get('freshness_sla_violations') or [])} "
            f"sla_unmeasured={len(quality.get('freshness_sla_unmeasured') or [])} "
            f"critical_ready={snapshot['llm_data_summary']['critical_data_ready']}",
            flush=True,
        )
        print(
            "SNAPSHOT_SCHEMA_UPGRADED schema_version=13 feature=data_policy:v1",
            flush=True,
        )

    data_metadata._metadata = metadata
    data_metadata._quality_summary = quality_summary
    data_metadata._llm_summary = llm_summary
    data_metadata.decorate_snapshot = decorate_snapshot
    data_metadata.finalize_snapshot = finalize_snapshot
    data_metadata._data_policy_bridge_installed = True
