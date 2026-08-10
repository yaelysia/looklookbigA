import argparse
import json
import os
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path


CST = timezone(timedelta(hours=8))


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(timezone.utc)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def manifest_revision(manifest):
    manifest = manifest or {}
    run_id = _as_int(manifest.get("latest_run_id"))
    attempt = _as_int(manifest.get("latest_run_attempt")) or 0
    dt = _parse_time(manifest.get("latest_runner_time_cst") or manifest.get("updated_at_cst"))
    timestamp = dt.timestamp() if dt else None
    return {
        "run_id": run_id,
        "attempt": attempt,
        "time": dt,
        "timestamp": timestamp,
    }


def compare_revisions(left_manifest, right_manifest):
    """Return -1/0/1 for left older/equal/newer than right."""
    left = manifest_revision(left_manifest)
    right = manifest_revision(right_manifest)
    if left["run_id"] is not None and right["run_id"] is not None:
        lk = (left["run_id"], left["attempt"], left["timestamp"] or 0)
        rk = (right["run_id"], right["attempt"], right["timestamp"] or 0)
    else:
        if left["timestamp"] is None or right["timestamp"] is None:
            raise ValueError("history revision is not comparable: missing run id and timestamp")
        lk = (left["timestamp"], left["run_id"] or 0, left["attempt"])
        rk = (right["timestamp"], right["run_id"] or 0, right["attempt"])
    return (lk > rk) - (lk < rk)


def _safe_snapshot(root, manifest):
    root = Path(root).resolve()
    rel = (manifest or {}).get("latest_snapshot")
    if not rel:
        raise ValueError("history manifest has no latest_snapshot")
    candidate = (root / str(rel)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("history manifest latest_snapshot escapes history root") from exc
    if not candidate.is_file():
        raise ValueError(f"history manifest latest_snapshot is missing: {rel}")
    return candidate


def validate_history_tree(root):
    root = Path(root)
    manifest = _load_json(root / "manifest.json")
    if not isinstance(manifest, dict):
        raise ValueError("history tree is missing a valid manifest.json")
    _safe_snapshot(root, manifest)
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"history tree contains a symlink: {path}")
    revision = manifest_revision(manifest)
    if revision["run_id"] is None and revision["timestamp"] is None:
        raise ValueError("history manifest has no usable revision evidence")
    return manifest


def _replace_tree(destination, source):
    destination = Path(destination)
    source = Path(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.parent / (destination.name + ".incoming")
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(source, staged, symlinks=False)
    if destination.exists():
        shutil.rmtree(destination)
    staged.replace(destination)


def hydrate_from_exact_artifact(current_root, artifact_root, expected_run_id=None):
    artifact_root = Path(artifact_root)
    incoming = validate_history_tree(artifact_root)
    incoming_run = manifest_revision(incoming)["run_id"]
    expected = _as_int(expected_run_id)
    if expected is not None and incoming_run is not None and incoming_run != expected:
        raise ValueError(
            f"exact history artifact run mismatch: expected={expected} manifest={incoming_run}"
        )

    current_root = Path(current_root)
    current = _load_json(current_root / "manifest.json") if current_root.exists() else None
    if isinstance(current, dict):
        try:
            if compare_revisions(incoming, current) < 0:
                print(
                    "HISTORY_BASELINE artifact_older_than_current "
                    f"artifact_run={incoming_run} current_run={manifest_revision(current)['run_id']}",
                    flush=True,
                )
                return False
        except ValueError:
            pass

    _replace_tree(current_root, artifact_root)
    selected = validate_history_tree(current_root)
    print(
        "HISTORY_BASELINE source=exact-previous-success-artifact "
        f"run_id={expected or manifest_revision(selected)['run_id']} "
        f"snapshot={selected.get('latest_snapshot')}",
        flush=True,
    )
    return True


def verify_fallback_at_least(current_root, expected_run_id=None, expected_started_at=None):
    manifest = validate_history_tree(current_root)
    revision = manifest_revision(manifest)
    expected = _as_int(expected_run_id)
    if expected is not None and revision["run_id"] is not None:
        if revision["run_id"] < expected:
            raise RuntimeError(
                f"market-data baseline is behind latest successful realtime run: "
                f"baseline={revision['run_id']} expected_at_least={expected}"
            )
        return True

    expected_time = _parse_time(expected_started_at)
    if expected_time is not None and revision["time"] is not None:
        if revision["time"] < expected_time:
            raise RuntimeError(
                "legacy market-data baseline timestamp predates latest successful realtime run"
            )
        return True

    if expected is not None or expected_time is not None:
        raise RuntimeError("cannot prove fallback history is at least the latest successful run")
    return True


def persist_if_newer(current_root, incoming_root, expected_run_id=None):
    current_root = Path(current_root)
    incoming_root = Path(incoming_root)
    incoming = validate_history_tree(incoming_root)
    incoming_revision = manifest_revision(incoming)
    expected = _as_int(expected_run_id)
    if expected is not None and incoming_revision["run_id"] is not None and incoming_revision["run_id"] != expected:
        raise ValueError(
            f"incoming persistence run mismatch: expected={expected} manifest={incoming_revision['run_id']}"
        )

    current = _load_json(current_root / "manifest.json") if current_root.exists() else None
    if isinstance(current, dict):
        try:
            ordering = compare_revisions(incoming, current)
        except ValueError:
            ordering = 1
        if ordering <= 0:
            print(
                "HISTORY_PERSIST monotonic_skip=true "
                f"incoming_run={incoming_revision['run_id']} "
                f"current_run={manifest_revision(current)['run_id']}",
                flush=True,
            )
            return False

    _replace_tree(current_root, incoming_root)
    persisted = validate_history_tree(current_root)
    persisted["persisted_run_id"] = expected or manifest_revision(persisted)["run_id"]
    persisted["persisted_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_json(current_root / "manifest.json", persisted)
    print(
        "HISTORY_PERSIST monotonic_write=true "
        f"run_id={persisted.get('persisted_run_id')} snapshot={persisted.get('latest_snapshot')}",
        flush=True,
    )
    return True


def install_manifest_revision(history_store):
    if getattr(history_store, "_run_revision_manifest_installed", False):
        return
    original = history_store._build_manifest

    def build_manifest(data, archive_rel):
        manifest = original(data, archive_rel)
        run_id = _as_int(os.environ.get("GITHUB_RUN_ID"))
        attempt = _as_int(os.environ.get("GITHUB_RUN_ATTEMPT")) or 1
        if run_id is not None:
            manifest["latest_run_id"] = run_id
            manifest["latest_run_attempt"] = attempt
        return manifest

    history_store._build_manifest = build_manifest
    history_store._run_revision_manifest_installed = True


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    hydrate = sub.add_parser("hydrate")
    hydrate.add_argument("--current", required=True)
    hydrate.add_argument("--incoming", required=True)
    hydrate.add_argument("--expected-run-id")

    verify = sub.add_parser("verify")
    verify.add_argument("--current", required=True)
    verify.add_argument("--expected-run-id")
    verify.add_argument("--expected-started-at")

    persist = sub.add_parser("persist")
    persist.add_argument("--current", required=True)
    persist.add_argument("--incoming", required=True)
    persist.add_argument("--expected-run-id")

    args = parser.parse_args(argv)
    if args.command == "hydrate":
        hydrate_from_exact_artifact(args.current, args.incoming, args.expected_run_id)
    elif args.command == "verify":
        verify_fallback_at_least(args.current, args.expected_run_id, args.expected_started_at)
    elif args.command == "persist":
        persist_if_newer(args.current, args.incoming, args.expected_run_id)


if __name__ == "__main__":
    main()
