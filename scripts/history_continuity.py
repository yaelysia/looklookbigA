import argparse
import contextlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import fcntl


CST = timezone(timedelta(hours=8))


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


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
        "head_sha": str(manifest.get("latest_head_sha") or "").lower() or None,
        "time": dt,
        "timestamp": timestamp,
    }


def compare_revisions(left_manifest, right_manifest):
    """Return -1/0/1 for left older/equal/newer than right.

    Snapshot time is the primary monotonic key. GitHub run id / attempt only
    disambiguate equal timestamps, so an older snapshot can never win merely
    because an implementation detail gave it a larger numeric run id.
    """
    left = manifest_revision(left_manifest)
    right = manifest_revision(right_manifest)
    if left["timestamp"] is not None and right["timestamp"] is not None:
        lk = (left["timestamp"], left["run_id"] or 0, left["attempt"])
        rk = (right["timestamp"], right["run_id"] or 0, right["attempt"])
    elif left["run_id"] is not None and right["run_id"] is not None:
        lk = (left["run_id"], left["attempt"])
        rk = (right["run_id"], right["attempt"])
    else:
        raise ValueError("history revision is not comparable: missing compatible time/run evidence")
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


def _validate_expected_identity(manifest, expected_run_id=None, expected_run_attempt=None, expected_head_sha=None):
    revision = manifest_revision(manifest)
    expected_run = _as_int(expected_run_id)
    expected_attempt = _as_int(expected_run_attempt)
    expected_sha = str(expected_head_sha or "").lower() or None
    if expected_run is not None and revision["run_id"] != expected_run:
        raise ValueError(
            f"history run mismatch: expected={expected_run} manifest={revision['run_id']}"
        )
    if expected_attempt is not None and revision["attempt"] != expected_attempt:
        raise ValueError(
            "history run attempt mismatch: "
            f"expected={expected_attempt} manifest={revision['attempt']}"
        )
    if expected_sha is not None and revision["head_sha"] != expected_sha:
        raise ValueError(
            f"history head SHA mismatch: expected={expected_sha} manifest={revision['head_sha']}"
        )
    return revision


def hydrate_from_exact_artifact(
    current_root,
    artifact_root,
    expected_run_id=None,
    expected_run_attempt=None,
    expected_head_sha=None,
):
    artifact_root = Path(artifact_root)
    incoming = validate_history_tree(artifact_root)
    incoming_revision = _validate_expected_identity(
        incoming, expected_run_id, expected_run_attempt, expected_head_sha
    )
    incoming_run = incoming_revision["run_id"]

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
        f"run_id={incoming_run} attempt={incoming_revision['attempt']} "
        f"snapshot={selected.get('latest_snapshot')}",
        flush=True,
    )
    return True


def verify_fallback_at_least(
    current_root,
    expected_run_id=None,
    expected_run_attempt=None,
    expected_head_sha=None,
    expected_started_at=None,
):
    manifest = validate_history_tree(current_root)
    revision = manifest_revision(manifest)
    expected = _as_int(expected_run_id)
    expected_attempt = _as_int(expected_run_attempt)
    expected_sha = str(expected_head_sha or "").lower() or None
    expected_time = _parse_time(expected_started_at)

    # New manifests can prove continuity with both run id and time. During the
    # first upgrade from schema-v2 legacy manifests, run id is absent, so a
    # timestamp at/after the exact previous successful run start is sufficient.
    if revision["run_id"] is not None and expected is not None:
        if revision["run_id"] < expected:
            raise RuntimeError(
                f"market-data baseline is behind latest successful realtime run: "
                f"baseline={revision['run_id']} expected_at_least={expected}"
            )
        if revision["run_id"] == expected and expected_attempt is not None:
            if revision["attempt"] < expected_attempt:
                raise RuntimeError(
                    "market-data baseline is behind expected workflow attempt: "
                    f"baseline={revision['attempt']} expected_at_least={expected_attempt}"
                )
            if (
                revision["attempt"] == expected_attempt
                and expected_sha is not None
                and revision["head_sha"] != expected_sha
            ):
                raise RuntimeError("market-data baseline exact attempt has a different head SHA")
        if expected_time is not None and revision["time"] is not None and revision["time"] < expected_time:
            raise RuntimeError(
                "market-data baseline timestamp predates latest successful realtime run"
            )
        return True

    if expected_time is not None and revision["time"] is not None:
        if revision["time"] < expected_time:
            raise RuntimeError(
                "legacy market-data baseline timestamp predates latest successful realtime run"
            )
        return True

    if expected is not None or expected_time is not None:
        raise RuntimeError("cannot prove fallback history is at least the latest successful run")
    return True


@contextlib.contextmanager
def _history_lock(root):
    root = Path(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    lock_path = root.parent / (root.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _recover_interrupted_swap(destination):
    destination = Path(destination)
    backup = destination.parent / (destination.name + ".previous")
    if not destination.exists() and backup.exists():
        backup.replace(destination)
    elif destination.exists() and backup.exists():
        shutil.rmtree(backup)
    for stale in destination.parent.glob(destination.name + ".merge.*"):
        if stale.is_dir():
            shutil.rmtree(stale)


def _commit_staged_tree(destination, staged):
    destination = Path(destination)
    staged = Path(staged)
    backup = destination.parent / (destination.name + ".previous")
    if backup.exists():
        shutil.rmtree(backup)
    if destination.exists():
        destination.replace(backup)
    try:
        staged.replace(destination)
    except Exception:
        if backup.exists() and not destination.exists():
            backup.replace(destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _merge_tree(current_root, incoming_root, staged_root, incoming_wins):
    current_root = Path(current_root)
    incoming_root = Path(incoming_root)
    staged_root = Path(staged_root)
    if current_root.exists():
        shutil.copytree(current_root, staged_root, symlinks=False, dirs_exist_ok=True)
    else:
        staged_root.mkdir(parents=True)

    changed = False
    for source in sorted(path for path in incoming_root.rglob("*") if path.is_file()):
        rel = source.relative_to(incoming_root)
        if rel == Path("manifest.json"):
            continue
        destination = staged_root / rel
        if not destination.exists():
            _copy_file(source, destination)
            changed = True
            continue
        if source.read_bytes() == destination.read_bytes():
            continue
        if rel.parts and rel.parts[0] == "snapshots":
            raise ValueError(f"immutable history archive conflict: {rel.as_posix()}")
        if rel.parts and rel.parts[0] == "minutes" and source.suffix == ".json":
            import minute_history

            current_document = _load_json(destination)
            incoming_document = _load_json(source)
            merged = minute_history.merge_documents(
                current_document,
                incoming_document,
                prefer="incoming" if incoming_wins else "current",
            )
            if merged != current_document:
                _write_json(destination, merged)
                changed = True
            continue
        if incoming_wins:
            _copy_file(source, destination)
            changed = True
    return changed


def persist_if_newer(
    current_root,
    incoming_root,
    expected_run_id=None,
    expected_run_attempt=None,
    expected_head_sha=None,
):
    current_root = Path(current_root)
    incoming_root = Path(incoming_root)
    incoming = validate_history_tree(incoming_root)
    incoming_revision = _validate_expected_identity(
        incoming, expected_run_id, expected_run_attempt, expected_head_sha
    )

    with _history_lock(current_root):
        _recover_interrupted_swap(current_root)
        current = _load_json(current_root / "manifest.json") if current_root.exists() else None
        if isinstance(current, dict):
            validate_history_tree(current_root)
            try:
                ordering = compare_revisions(incoming, current)
            except ValueError:
                ordering = 1
            if ordering == 0:
                current_revision = manifest_revision(current)
                if (
                    incoming.get("latest_snapshot") != current.get("latest_snapshot")
                    or incoming_revision["head_sha"] != current_revision["head_sha"]
                ):
                    raise ValueError("equal history revisions claim contradictory latest snapshots")
        else:
            ordering = 1

        current_root.parent.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix=current_root.name + ".merge.", dir=current_root.parent))
        try:
            changed = _merge_tree(
                current_root,
                incoming_root,
                staged,
                incoming_wins=ordering > 0,
            )
            winner = dict(incoming if ordering > 0 or not isinstance(current, dict) else current)
            winner_changed = not isinstance(current, dict) or ordering > 0
            changed = changed or winner_changed
            if not changed:
                shutil.rmtree(staged)
                print(
                    "HISTORY_PERSIST idempotent_skip=true "
                    f"incoming_run={incoming_revision['run_id']} attempt={incoming_revision['attempt']}",
                    flush=True,
                )
                return False
            winner["schema_version"] = max(int(winner.get("schema_version") or 0), 3)
            winner["last_merged_run_id"] = incoming_revision["run_id"]
            winner["last_merged_run_attempt"] = incoming_revision["attempt"]
            winner["persisted_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _write_json(staged / "manifest.json", winner)
            validate_history_tree(staged)
            _commit_staged_tree(current_root, staged)
        finally:
            if staged.exists():
                shutil.rmtree(staged)

    persisted = validate_history_tree(current_root)
    print(
        "HISTORY_PERSIST monotonic_merge=true "
        f"latest_run={manifest_revision(persisted)['run_id']} "
        f"merged_run={incoming_revision['run_id']} snapshot={persisted.get('latest_snapshot')}",
        flush=True,
    )
    return True


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    run_id = _as_int(os.environ.get("GITHUB_RUN_ID"))
    attempt = _as_int(os.environ.get("GITHUB_RUN_ATTEMPT")) or 1
    head_sha = str(os.environ.get("GITHUB_SHA") or "").lower() or None
    data["observation"] = {
        "runner_time_cst": data.get("runner_time_cst"),
        "runner_time_utc": data.get("runner_time_utc"),
        "run_id": run_id if run_id is not None else "local",
        "run_attempt": attempt,
        "head_sha": head_sha,
        "source": "GITHUB_ACTIONS" if run_id is not None else "LOCAL",
    }
    data["schema_version"] = max(int(data.get("schema_version") or 0), 19)
    data.setdefault("features", {})["observation_identity"] = "v1"
    _write_json(path, data)
    print(
        "OBSERVATION_IDENTITY "
        f"run_id={data['observation']['run_id']} attempt={attempt} head_sha={head_sha}",
        flush=True,
    )


def install_manifest_revision(history_store):
    if getattr(history_store, "_run_revision_manifest_installed", False):
        return
    original = history_store._build_manifest

    def build_manifest(data, archive_rel):
        manifest = original(data, archive_rel)
        observation = data.get("observation") or {}
        run_id = _as_int(observation.get("run_id") or os.environ.get("GITHUB_RUN_ID"))
        attempt = _as_int(
            observation.get("run_attempt") or os.environ.get("GITHUB_RUN_ATTEMPT")
        ) or 1
        if run_id is not None:
            manifest["latest_run_id"] = run_id
            manifest["latest_run_attempt"] = attempt
            manifest["latest_head_sha"] = observation.get("head_sha") or os.environ.get("GITHUB_SHA")
            if observation:
                manifest["latest_observation"] = dict(observation)
        manifest["schema_version"] = max(int(manifest.get("schema_version") or 0), 3)
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
    hydrate.add_argument("--expected-run-attempt")
    hydrate.add_argument("--expected-head-sha")

    verify = sub.add_parser("verify")
    verify.add_argument("--current", required=True)
    verify.add_argument("--expected-run-id")
    verify.add_argument("--expected-run-attempt")
    verify.add_argument("--expected-head-sha")
    verify.add_argument("--expected-started-at")

    persist = sub.add_parser("persist")
    persist.add_argument("--current", required=True)
    persist.add_argument("--incoming", required=True)
    persist.add_argument("--expected-run-id")
    persist.add_argument("--expected-run-attempt")
    persist.add_argument("--expected-head-sha")

    args = parser.parse_args(argv)
    if args.command == "hydrate":
        hydrate_from_exact_artifact(
            args.current,
            args.incoming,
            args.expected_run_id,
            args.expected_run_attempt,
            args.expected_head_sha,
        )
    elif args.command == "verify":
        verify_fallback_at_least(
            args.current,
            args.expected_run_id,
            args.expected_run_attempt,
            args.expected_head_sha,
            args.expected_started_at,
        )
    elif args.command == "persist":
        persist_if_newer(
            args.current,
            args.incoming,
            args.expected_run_id,
            args.expected_run_attempt,
            args.expected_head_sha,
        )


if __name__ == "__main__":
    main()
