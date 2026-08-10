import json
import os
import tempfile
from pathlib import Path

import alpha_artifact_manifest
import alpha_capability_contract
import alpha_refresh_contract


def _snapshot():
    return {
        "schema_version": 15,
        "features": {
            "intraday_structure_metrics": "v1",
            "daily_k_context": "v1",
            "live_price_guard": "v1",
            "market_environment": "v1",
            "data_provenance": "v1",
            "changes_since_previous": "v1",
            "company_events": "v1",
            "capital_flow": "v1",
            "fundamentals": "v1",
        },
        "detail_stocks": {
            "002558": {
                "quote": {},
                "intraday": {"current_price_guard": {"status": "OK"}},
                "daily_context": {},
                "events": {},
                "capital_flow": {},
                "fundamentals": {},
            }
        },
        "groups": {},
        "indices": {},
        "live_price_guard": {
            "policy": {"historical_sources_allowed_for_current_price": False}
        },
        "market_environment": {},
        "data_quality": {},
        "changes_since_previous": {},
        "company_events": {},
        "capital_flow_summary": {},
        "fundamentals_summary": {},
    }


def test_capability_manifest_accepts_full_profile():
    manifest = alpha_capability_contract.load_manifest()
    errors = alpha_capability_contract.validate_snapshot(_snapshot(), manifest)
    assert errors == [], errors
    print("PASS alpha_capability_full_profile")


def test_capability_manifest_rejects_missing_guard():
    snapshot = _snapshot()
    snapshot["detail_stocks"]["002558"]["intraday"].pop("current_price_guard")
    errors = alpha_capability_contract.validate_snapshot(snapshot)
    assert any("current_price_guard" in error for error in errors), errors
    print("PASS alpha_capability_rejects_missing_guard")


def test_artifact_manifest_has_exact_digest_and_single_primary():
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "snapshot.json"
        output = Path(tmp) / "alpha-artifact-manifest.json"
        snapshot.write_text('{"schema_version":15}\n', encoding="utf-8")
        old = os.environ.get("GITHUB_RUN_ID")
        os.environ["GITHUB_RUN_ID"] = "12345"
        try:
            payload = alpha_artifact_manifest.write_manifest(snapshot, output)
        finally:
            if old is None:
                os.environ.pop("GITHUB_RUN_ID", None)
            else:
                os.environ["GITHUB_RUN_ID"] = old
        loaded = json.loads(output.read_text(encoding="utf-8"))
        assert loaded == payload
        assert loaded["primary_artifact_count"] == 1
        assert loaded["primary"]["workflow_run_id"] == "12345"
        assert loaded["primary"]["size_bytes"] == snapshot.stat().st_size
        assert loaded["primary"]["digest"] == alpha_artifact_manifest.sha256_file(snapshot)
        assert len(loaded["primary"]["digest"]) == 64
    print("PASS alpha_primary_artifact_digest")


def test_refresh_identity_same_command_recovers_exact_run():
    digest = alpha_refresh_contract.canonical_command_digest(
        "realtime-quotes.yml@sha",
        {"mode": "FULL", "config": "watchlist-a"},
    )
    title = alpha_refresh_contract.build_run_name("corr-1", "idem-1", digest)
    run = {
        "id": 77,
        "display_title": title,
        "status": "completed",
        "conclusion": "success",
        "head_sha": "a" * 40,
        "run_attempt": 1,
        "html_url": "https://example.invalid/run/77",
    }
    resolved = alpha_refresh_contract.resolve_operation_by_correlation(
        [run], "corr-1", "idem-1", digest
    )
    assert resolved["workflow_run_id"] == 77
    assert resolved["status"] == "SUCCEEDED"
    print("PASS alpha_refresh_exact_recovery")


def test_refresh_identity_rejects_conflicting_reuse():
    digest_a = "a" * 64
    digest_b = "b" * 64
    existing = {
        "id": 1,
        "display_title": alpha_refresh_contract.build_run_name(
            "corr-1", "idem-1", digest_a
        ),
        "status": "completed",
        "conclusion": "success",
    }
    try:
        alpha_refresh_contract.resolve_operation_by_correlation(
            [existing], "corr-1", "idem-1", digest_b
        )
    except alpha_refresh_contract.RefreshIdentityConflict:
        pass
    else:
        raise AssertionError("conflicting correlation/key reuse must be rejected")
    print("PASS alpha_refresh_conflict_rejected")


def test_refresh_identity_detects_duplicate_exact_runs():
    digest = "c" * 64
    title = alpha_refresh_contract.build_run_name("corr-2", "idem-2", digest)
    runs = [
        {"id": 1, "display_title": title, "status": "queued", "conclusion": None},
        {"id": 2, "display_title": title, "status": "in_progress", "conclusion": None},
    ]
    try:
        alpha_refresh_contract.resolve_operation_by_correlation(
            runs, "corr-2", "idem-2", digest
        )
    except alpha_refresh_contract.RefreshOperationAmbiguous:
        pass
    else:
        raise AssertionError("duplicate exact runs must never be guessed between")
    print("PASS alpha_refresh_duplicate_exact_runs_rejected")


def main():
    tests = [
        test_capability_manifest_accepts_full_profile,
        test_capability_manifest_rejects_missing_guard,
        test_artifact_manifest_has_exact_digest_and_single_primary,
        test_refresh_identity_same_command_recovers_exact_run,
        test_refresh_identity_rejects_conflicting_reuse,
        test_refresh_identity_detects_duplicate_exact_runs,
    ]
    for test in tests:
        test()
    print(f"ALPHA_PROVIDER_CONTRACT_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
