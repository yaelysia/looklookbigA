import json
from pathlib import Path

import upcoming_events_changes


def recount(changes):
    if not isinstance(changes, dict):
        return changes
    items = []
    items.extend((changes.get("stocks") or {}).values())
    items.extend((changes.get("groups") or {}).values())
    for key in ("market", "events"):
        value = changes.get(key)
        if isinstance(value, dict):
            items.append(value)
    counts = {"SIGNIFICANT": 0, "MODERATE": 0, "MINOR": 0}
    for item in items:
        severity = str((item or {}).get("significance") or "NONE").upper()
        if severity in counts:
            counts[severity] += 1
    summary = changes.setdefault("summary", {})
    summary["significant_changes"] = counts["SIGNIFICANT"]
    summary["moderate_changes"] = counts["MODERATE"]
    summary["minor_changes"] = counts["MINOR"]
    return changes


def finalize_snapshot(snapshot_path):
    upcoming_events_changes.finalize_snapshot(snapshot_path)
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    changes = snapshot.get("changes_since_previous")
    if isinstance(changes, dict):
        recount(changes)
        snapshot.setdefault("features", {})["changes_summary_finalizer"] = "v1"
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = changes.get("summary") or {}
        print(
            "CHANGES_SUMMARY_FINAL "
            f"significant={summary.get('significant_changes', 0)} "
            f"moderate={summary.get('moderate_changes', 0)} "
            f"minor={summary.get('minor_changes', 0)}",
            flush=True,
        )
