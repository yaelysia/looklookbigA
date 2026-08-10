import json
import os
import tempfile
from pathlib import Path

import history_continuity
import history_store
import resolve_previous_realtime_run


def _history_tree(root, run_id, runner_time, marker, attempt=1, include_run_id=True):
    root = Path(root)
    rel = Path("snapshots") / runner_time[:10] / f"{marker}.json"
    snapshot = {
        "schema_version": 13,
        "runner_time_cst": runner_time,
        "detail_stocks": {},
        "marker": marker,
    }
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(json.dumps(snapshot), encoding="utf-8")
    manifest = {
        "schema_version": 2,
        "latest_snapshot": str(rel).replace("\\", "/"),
        "latest_runner_time_cst": runner_time,
        "updated_at_cst": runner_time,
        "daily_k_codes": [],
    }
    if include_run_id:
        manifest["latest_run_id"] = run_id
        manifest["latest_run_attempt"] = attempt
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest, snapshot


def test_run_b_reads_exact_run_a_artifact_before_async_persist():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        branch_history = base / "market-data-history"
        exact_a = base / "run-a-artifact"
        _history_tree(branch_history, 100, "2026-08-10 10:00:00", "run100")
        manifest_a, _ = _history_tree(exact_a, 101, "2026-08-10 10:01:00", "run101")

        # Run A has succeeded and published its immutable artifact, but async
        # Persist A has not run: market-data is deliberately still run 100.
        assert json.loads((branch_history / "manifest.json").read_text())["latest_run_id"] == 100
        changed = history_continuity.hydrate_from_exact_artifact(
            branch_history, exact_a, expected_run_id=101
        )
        assert changed is True
        selected = json.loads((branch_history / "manifest.json").read_text())
        assert selected["latest_run_id"] == 101
        assert selected["latest_snapshot"] == manifest_a["latest_snapshot"]

        old_root = os.environ.get("MARKET_HISTORY_DIR")
        os.environ["MARKET_HISTORY_DIR"] = str(branch_history)
        try:
            current_b = base / "run-b-snapshot.json"
            current_b.write_text(
                json.dumps({
                    "schema_version": 13,
                    "runner_time_cst": "2026-08-10 10:02:00",
                    "detail_stocks": {},
                }),
                encoding="utf-8",
            )
            history_store.CACHE_META.clear()
            history_store.finalize_snapshot(current_b)
            prepared = json.loads(current_b.read_text(encoding="utf-8"))
            assert prepared["history"]["previous_snapshot_path"] == manifest_a["latest_snapshot"]
            previous, previous_path = history_store.load_previous_snapshot(prepared)
            assert previous_path == manifest_a["latest_snapshot"]
            assert previous["marker"] == "run101"
        finally:
            history_store.CACHE_META.clear()
            if old_root is None:
                os.environ.pop("MARKET_HISTORY_DIR", None)
            else:
                os.environ["MARKET_HISTORY_DIR"] = old_root


def test_out_of_order_persistence_cannot_regress_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        current = base / "current"
        older = base / "older"
        newer = base / "newer"
        misleading_id = base / "misleading-id"
        _history_tree(current, 102, "2026-08-10 10:02:00", "run102")
        _history_tree(older, 101, "2026-08-10 10:01:00", "run101")
        _history_tree(newer, 103, "2026-08-10 10:03:00", "run103")
        _history_tree(misleading_id, 999, "2026-08-10 09:59:00", "run999-old-time")

        assert history_continuity.persist_if_newer(current, older, expected_run_id=101) is False
        assert json.loads((current / "manifest.json").read_text())["latest_run_id"] == 102

        # Time is the primary monotonic key: a numerically larger run id with
        # an older snapshot is also forbidden from rolling history backward.
        assert history_continuity.persist_if_newer(current, misleading_id, expected_run_id=999) is False
        assert json.loads((current / "manifest.json").read_text())["latest_run_id"] == 102

        assert history_continuity.persist_if_newer(current, newer, expected_run_id=103) is True
        final_manifest = json.loads((current / "manifest.json").read_text())
        assert final_manifest["latest_run_id"] == 103
        assert final_manifest["persisted_run_id"] == 103
        assert "persisted_at_utc" in final_manifest


def test_legacy_fallback_can_prove_continuity_by_time():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "legacy"
        _history_tree(
            root,
            0,
            "2026-08-10 10:05:00",
            "legacy",
            include_run_id=False,
        )
        assert history_continuity.verify_fallback_at_least(
            root,
            expected_run_id=500,
            expected_started_at="2026-08-10T02:04:30Z",
        ) is True
        try:
            history_continuity.verify_fallback_at_least(
                root,
                expected_run_id=500,
                expected_started_at="2026-08-10T02:06:00Z",
            )
        except RuntimeError as exc:
            assert "predates" in str(exc)
        else:
            raise AssertionError("older legacy fallback must be rejected")


def test_manifest_records_exact_workflow_revision():
    history_continuity.install_manifest_revision(history_store)
    old_id = os.environ.get("GITHUB_RUN_ID")
    old_attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
    os.environ["GITHUB_RUN_ID"] = "123456"
    os.environ["GITHUB_RUN_ATTEMPT"] = "2"
    try:
        manifest = history_store._build_manifest(
            {"runner_time_cst": "2026-08-10 10:10:00"},
            Path("snapshots/2026-08-10/example.json"),
        )
        assert manifest["latest_run_id"] == 123456
        assert manifest["latest_run_attempt"] == 2
    finally:
        if old_id is None:
            os.environ.pop("GITHUB_RUN_ID", None)
        else:
            os.environ["GITHUB_RUN_ID"] = old_id
        if old_attempt is None:
            os.environ.pop("GITHUB_RUN_ATTEMPT", None)
        else:
            os.environ["GITHUB_RUN_ATTEMPT"] = old_attempt


def test_previous_run_resolver_selects_latest_successful_master_run():
    payload = {
        "workflow_runs": [
            {"id": 105, "conclusion": "success", "head_branch": "feature/x", "path": ".github/workflows/realtime-quotes.yml", "run_started_at": "2026-08-10T02:05:00Z"},
            {"id": 104, "conclusion": "failure", "head_branch": "master", "path": ".github/workflows/realtime-quotes.yml", "run_started_at": "2026-08-10T02:04:00Z"},
            {"id": 103, "conclusion": "success", "head_branch": "master", "path": ".github/workflows/realtime-quotes.yml", "run_started_at": "2026-08-10T02:03:00Z", "head_sha": "b" * 40},
            {"id": 102, "conclusion": "success", "head_branch": "master", "path": ".github/workflows/realtime-quotes.yml", "run_started_at": "2026-08-10T02:02:00Z", "head_sha": "a" * 40},
        ]
    }
    selected = resolve_previous_realtime_run.select_previous_success(payload, current_run_id=106)
    assert selected["id"] == 103
    assert selected["head_sha"] == "b" * 40


def main():
    tests = [
        test_run_b_reads_exact_run_a_artifact_before_async_persist,
        test_out_of_order_persistence_cannot_regress_manifest,
        test_legacy_fallback_can_prove_continuity_by_time,
        test_manifest_records_exact_workflow_revision,
        test_previous_run_resolver_selects_latest_successful_master_run,
    ]
    for test in tests:
        test()
    print(f"HISTORY_CONTINUITY_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
