import io
import json
import tempfile
import zipfile
from pathlib import Path

import history_artifact


def _archive(run_id, attempt, head_sha, marker):
    rel = f"snapshots/2026-08-10/{marker}.json"
    manifest = {
        "schema_version": 3,
        "latest_snapshot": rel,
        "latest_runner_time_cst": f"2026-08-10 10:0{attempt}:00",
        "latest_run_id": run_id,
        "latest_run_attempt": attempt,
        "latest_head_sha": head_sha,
    }
    snapshot = {
        "schema_version": 19,
        "runner_time_cst": manifest["latest_runner_time_cst"],
        "observation": {
            "run_id": run_id,
            "run_attempt": attempt,
            "head_sha": head_sha,
        },
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(rel, json.dumps(snapshot))
    return output.getvalue()


def _legacy_archive(run_id, attempt, marker):
    rel = f"snapshots/2026-08-10/{marker}.json"
    manifest = {
        "schema_version": 2,
        "latest_snapshot": rel,
        "latest_runner_time_cst": "2026-08-10 10:00:00",
        "latest_run_id": run_id,
        "latest_run_attempt": attempt,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(rel, json.dumps({"schema_version": 16, "runner_time_cst": "2026-08-10 10:00:00"}))
    return output.getvalue()


def test_exact_attempt_is_selected_from_duplicate_artifact_names():
    sha = "a" * 40
    artifacts = [
        {"id": 10, "name": "market-history-state", "expired": False},
        {"id": 11, "name": "market-history-state", "expired": False},
    ]
    archives = {
        10: _archive(500, 1, sha, "attempt1"),
        11: _archive(500, 2, sha, "attempt2"),
    }
    selected, _, manifest = history_artifact.select_exact_archive(
        artifacts,
        lambda artifact: archives[artifact["id"]],
        500,
        1,
        sha,
    )
    assert selected["id"] == 10
    assert manifest["latest_run_attempt"] == 1


def test_contradictory_or_ambiguous_exact_artifact_fails_closed():
    sha = "b" * 40
    exact = _archive(700, 2, sha, "attempt2")
    artifacts = [
        {"id": 20, "name": "market-history-state", "expired": False},
        {"id": 21, "name": "market-history-state", "expired": False},
    ]
    try:
        history_artifact.select_exact_archive(
            artifacts,
            lambda artifact: exact,
            700,
            2,
            sha,
        )
    except history_artifact.HistoryArtifactError as exc:
        assert "found=2" in str(exc)
    else:
        raise AssertionError("multiple exact-attempt artifacts must be rejected")

    try:
        history_artifact.inspect_archive(exact, 700, 2, "c" * 40)
    except ValueError as exc:
        assert "head SHA mismatch" in str(exc)
    else:
        raise AssertionError("contradictory head SHA must be rejected")


def test_restore_exact_history_installs_validated_tree():
    sha = "d" * 40
    artifact = {
        "id": 30,
        "name": "market-history-state",
        "expired": False,
        "archive_download_url": "https://api.github.com/artifacts/30/zip",
    }
    archive_bytes = _archive(900, 3, sha, "attempt3")
    original_list = history_artifact.alpha_primary_artifact.list_exact_run_artifacts
    original_download = history_artifact._download_archive
    history_artifact.alpha_primary_artifact.list_exact_run_artifacts = (
        lambda repository, token, run_id: [artifact]
    )
    history_artifact._download_archive = lambda url, token: archive_bytes
    try:
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "history"
            history_artifact.restore_exact_history(
                current,
                "owner/repo",
                "token",
                900,
                3,
                sha,
            )
            manifest = json.loads((current / "manifest.json").read_text(encoding="utf-8"))
            assert manifest["latest_run_id"] == 900
            assert manifest["latest_run_attempt"] == 3
            assert (current / manifest["latest_snapshot"]).is_file()
    finally:
        history_artifact.alpha_primary_artifact.list_exact_run_artifacts = original_list
        history_artifact._download_archive = original_download


def test_legacy_schema2_artifact_is_accepted_only_for_exact_run_attempt():
    sha = "f" * 40
    artifact = {
        "id": 40,
        "name": "market-history-state",
        "expired": False,
        "workflow_run": {"id": 1000, "head_sha": sha},
    }
    selected, _, manifest = history_artifact.select_exact_archive(
        [artifact],
        lambda item: _legacy_archive(1000, 1, "legacy"),
        1000,
        1,
        sha,
    )
    assert selected["id"] == 40
    assert manifest["legacy_identity"] is True

    try:
        history_artifact.select_exact_archive(
            [artifact],
            lambda item: _legacy_archive(1000, 2, "wrong-attempt"),
            1000,
            1,
            sha,
        )
    except history_artifact.HistoryArtifactError as exc:
        assert "found=0" in str(exc)
    else:
        raise AssertionError("legacy artifact with wrong attempt must be rejected")


def main():
    tests = [
        test_exact_attempt_is_selected_from_duplicate_artifact_names,
        test_contradictory_or_ambiguous_exact_artifact_fails_closed,
        test_restore_exact_history_installs_validated_tree,
        test_legacy_schema2_artifact_is_accepted_only_for_exact_run_attempt,
    ]
    for test in tests:
        test()
    print(f"HISTORY_ARTIFACT_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
