def install(data_metadata):
    if getattr(data_metadata, "_changes_metadata_bridge_installed", False):
        return

    original = data_metadata._decorate_system_nodes

    def decorate_system_nodes_with_changes(snapshot, fetched_at):
        original(snapshot, fetched_at)

        history = snapshot.get("history")
        if isinstance(history, dict) and not history.get("manifest"):
            previous_manifest = history.get("previous_manifest") or {}
            if previous_manifest:
                history["metadata"] = data_metadata._metadata(
                    "market-data branch",
                    fetched_at,
                    data_time=data_metadata._market_time_iso(
                        previous_manifest.get("latest_runner_time_cst")
                    ),
                    freshness="HISTORICAL",
                    freshness_policy="CACHE_HISTORY",
                    quality="PASS",
                    source_type="CACHE",
                    source_tier="CACHE",
                    quality_flags=["NOT_A_REALTIME_SOURCE"],
                )

        changes = snapshot.get("changes_since_previous")
        if not isinstance(changes, dict):
            return

        status = changes.get("status")
        quality = (
            "PASS"
            if str(status or "").upper() == "AVAILABLE"
            else data_metadata._quality_from_status(status)
        )
        baseline = changes.get("baseline") or {}
        flags = list(baseline.get("quality_flags") or [])
        if status == "NO_BASELINE":
            flags.append("NO_COMPARISON_BASELINE")

        changes["metadata"] = data_metadata._derived_metadata(
            fetched_at,
            quality=quality,
            freshness="DERIVED_CURRENT",
            flags=flags,
            data_time=baseline.get("current_snapshot_time"),
        )
        changes["provenance"] = {
            "type": "DERIVED",
            "derived_from": [
                "history.previous_snapshot_path",
                "detail_stocks",
                "groups",
                "indices",
                "market_environment",
                "detail_stocks.*.events",
            ],
            "algorithm": "changes_since_previous_v1",
            "baseline_snapshot_path": baseline.get("previous_snapshot_path"),
            "interval_seconds": baseline.get("interval_seconds"),
        }

    data_metadata._decorate_system_nodes = decorate_system_nodes_with_changes
    data_metadata._changes_metadata_bridge_installed = True
