import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import alpha_artifact_manifest
import alpha_capability_contract
import alpha_operation_registry
import alpha_provider_contract
import alpha_refresh_contract


def _snapshot():
    return {
        "schema_version": 16,
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
            "alpha_provider_contract": "v1",
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


def _identity(correlation="corr-1", key="idem-1", mode="full"):
    workflow = "realtime-quotes.yml@" + "a" * 40
    material = {"mode": mode}
    return {
        "dispatch_correlation_id": correlation,
        "idempotency_key": key,
        "workflow_identity": workflow,
        "material_execution_inputs": material,
        "command_digest": alpha_refresh_contract.canonical_command_digest(
            workflow, material
        ),
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
        snapshot.write_text('{"schema_version":16}\n', encoding="utf-8")
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


def test_artifact_manifest_allows_exact_engine_revision_override():
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "snapshot.json"
        snapshot.write_text('{"schema_version":16}\n', encoding="utf-8")
        names = {
            "LOOKLOOK_PRODUCING_REF": "a" * 40,
            "LOOKLOOK_PRODUCING_REF_NAME": "a" * 40,
            "LOOKLOOK_PRODUCING_COMMIT_SHA": "a" * 40,
            "LOOKLOOK_WORKFLOW_SHA": "a" * 40,
        }
        old = {name: os.environ.get(name) for name in names}
        try:
            os.environ.update(names)
            payload = alpha_artifact_manifest.build_manifest(snapshot, "fixture")
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        primary = payload["primary"]
        assert primary["artifact_name"] == "fixture"
        assert primary["producing_commit_sha"] == "a" * 40
        assert primary["workflow_sha"] == "a" * 40
    print("PASS alpha_primary_artifact_engine_revision_override")


def test_refresh_identity_same_command_recovers_exact_run():
    identity = _identity()
    title = alpha_refresh_contract.build_run_name(
        identity["dispatch_correlation_id"], identity["command_digest"]
    )
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
        [run],
        identity["dispatch_correlation_id"],
        identity["idempotency_key"],
        identity["command_digest"],
    )
    assert resolved["workflow_run_id"] == 77
    assert resolved["status"] == "SUCCEEDED"
    print("PASS alpha_refresh_exact_recovery")


def test_refresh_identity_rejects_correlation_digest_conflict():
    digest_a = "a" * 64
    digest_b = "b" * 64
    existing = {
        "id": 1,
        "display_title": alpha_refresh_contract.build_run_name("corr-1", digest_a),
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
        raise AssertionError("conflicting correlation reuse must be rejected")
    print("PASS alpha_refresh_correlation_conflict_rejected")


def test_refresh_identity_detects_duplicate_exact_runs():
    digest = "c" * 64
    title = alpha_refresh_contract.build_run_name("corr-2", digest)
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


def test_refresh_terminal_states_are_machine_distinguishable():
    assert alpha_refresh_contract.operation_state(
        {"status": "queued", "conclusion": None}
    ) == "QUEUED"
    assert alpha_refresh_contract.operation_state(
        {"status": "in_progress", "conclusion": None}
    ) == "RUNNING"
    assert alpha_refresh_contract.operation_state(
        {"status": "completed", "conclusion": "success"}
    ) == "SUCCEEDED"
    assert alpha_refresh_contract.operation_state(
        {"status": "completed", "conclusion": "cancelled"}
    ) == "CANCELLED"
    assert alpha_refresh_contract.operation_state(
        {"status": "completed", "conclusion": "failure"}
    ) == "FAILED"
    print("PASS alpha_refresh_terminal_states")


def test_registry_same_key_same_command_recovers_one_operation():
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    registry = alpha_operation_registry.empty_registry()
    identity = _identity("corr-a", "idem-a")
    registry, first, changed, should_dispatch = alpha_operation_registry.reserve_state(
        registry, owner_token="worker-a", now=now, **identity
    )
    assert changed and should_dispatch

    alias = dict(identity)
    alias["dispatch_correlation_id"] = "corr-b"
    registry, second, changed, should_dispatch = alpha_operation_registry.reserve_state(
        registry, owner_token="worker-b", now=now + timedelta(seconds=1), **alias
    )
    assert first["operation_id"] == second["operation_id"]
    assert changed is True
    assert should_dispatch is False
    assert sorted(second["correlation_ids"]) == ["corr-a", "corr-b"]
    print("PASS alpha_registry_same_key_same_command_dedup")


def test_registry_same_key_different_command_conflicts():
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    registry = alpha_operation_registry.empty_registry()
    first = _identity("corr-a", "idem-a", "full")
    registry, _, _, _ = alpha_operation_registry.reserve_state(
        registry, owner_token="worker-a", now=now, **first
    )
    second = _identity("corr-b", "idem-a", "intraday_fast")
    try:
        alpha_operation_registry.reserve_state(
            registry, owner_token="worker-b", now=now, **second
        )
    except alpha_refresh_contract.RefreshIdentityConflict:
        pass
    else:
        raise AssertionError("same key with a different command must conflict")
    print("PASS alpha_registry_key_conflict")


def test_registry_correlation_different_command_conflicts():
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    registry = alpha_operation_registry.empty_registry()
    first = _identity("corr-a", "idem-a", "full")
    registry, _, _, _ = alpha_operation_registry.reserve_state(
        registry, owner_token="worker-a", now=now, **first
    )
    second = _identity("corr-a", "idem-b", "intraday_fast")
    try:
        alpha_operation_registry.reserve_state(
            registry, owner_token="worker-b", now=now, **second
        )
    except alpha_refresh_contract.RefreshIdentityConflict:
        pass
    else:
        raise AssertionError("correlation reused for a different command must conflict")
    print("PASS alpha_registry_correlation_conflict")


def test_registry_claims_exact_run_and_rejects_duplicate_dispatch():
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    registry = alpha_operation_registry.empty_registry()
    identity = _identity("corr-run", "idem-run")
    registry, reserved, _, _ = alpha_operation_registry.reserve_state(
        registry, owner_token="dispatcher", now=now, **identity
    )
    registry, claimed, _ = alpha_operation_registry.claim_run_state(
        registry,
        workflow_run_id=101,
        workflow_run_url="https://example.invalid/101",
        workflow_head_sha="b" * 40,
        workflow_run_attempt=1,
        now=now + timedelta(seconds=5),
        **identity,
    )
    assert claimed["operation_id"] == reserved["operation_id"]
    assert claimed["workflow_run_id"] == 101
    assert claimed["dispatch_state"] == "DISPATCHED"
    try:
        alpha_operation_registry.claim_run_state(
            registry,
            workflow_run_id=102,
            now=now + timedelta(seconds=6),
            **identity,
        )
    except alpha_operation_registry.RegistryConflict:
        pass
    else:
        raise AssertionError("a second run must not claim the same logical operation")
    print("PASS alpha_registry_exact_run_claim")


def test_registry_distinct_commands_never_cross_wire():
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    registry = alpha_operation_registry.empty_registry()
    first = _identity("corr-x", "idem-x", "full")
    second = _identity("corr-y", "idem-y", "intraday_fast")
    registry, op_x, _, _ = alpha_operation_registry.reserve_state(
        registry, owner_token="x", now=now, **first
    )
    registry, op_y, _, _ = alpha_operation_registry.reserve_state(
        registry, owner_token="y", now=now, **second
    )
    assert op_x["operation_id"] != op_y["operation_id"]
    assert alpha_operation_registry.resolve_state(registry, **first)["operation_id"] == op_x["operation_id"]
    assert alpha_operation_registry.resolve_state(registry, **second)["operation_id"] == op_y["operation_id"]
    print("PASS alpha_registry_concurrent_identity_isolation")


def test_registry_expired_reservation_never_redispatches_without_scan():
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    registry = alpha_operation_registry.empty_registry()
    identity = _identity("corr-expired", "idem-expired")
    registry, _, _, should_dispatch = alpha_operation_registry.reserve_state(
        registry, owner_token="first", now=now, **identity
    )
    assert should_dispatch is True

    after_expiry = now + timedelta(
        seconds=alpha_operation_registry.DISPATCH_LEASE_SECONDS + 1
    )
    registry, record, changed, should_dispatch = alpha_operation_registry.reserve_state(
        registry, owner_token="second", now=after_expiry, **identity
    )
    assert changed is False
    assert should_dispatch is False
    assert record["workflow_run_id"] is None
    print("PASS alpha_registry_expired_reservation_requires_scan")


def test_registry_correlation_scan_recovers_exact_existing_run():
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    registry = alpha_operation_registry.empty_registry()
    identity = _identity("corr-recover", "idem-recover")
    registry, _, _, _ = alpha_operation_registry.reserve_state(
        registry, owner_token="first", now=now, **identity
    )
    after_expiry = now + timedelta(
        seconds=alpha_operation_registry.DISPATCH_LEASE_SECONDS + 1
    )
    run = {
        "id": 404,
        "display_title": alpha_refresh_contract.build_run_name(
            identity["dispatch_correlation_id"], identity["command_digest"]
        ),
        "status": "completed",
        "conclusion": "success",
        "head_sha": "d" * 40,
        "run_attempt": 1,
        "html_url": "https://example.invalid/run/404",
    }
    registry, record, changed, should_dispatch = (
        alpha_operation_registry.recover_after_correlation_scan_state(
            registry,
            [run],
            owner_token="second",
            now=after_expiry,
            **identity,
        )
    )
    assert changed is True
    assert should_dispatch is False
    assert record["workflow_run_id"] == 404
    assert record["dispatch_state"] == "DISPATCHED"
    print("PASS alpha_registry_ambiguous_dispatch_recovers_exact_run")


def test_registry_verified_empty_scan_can_authorize_redispatch():
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    registry = alpha_operation_registry.empty_registry()
    identity = _identity("corr-empty", "idem-empty")
    registry, _, _, _ = alpha_operation_registry.reserve_state(
        registry, owner_token="first", now=now, **identity
    )
    after_expiry = now + timedelta(
        seconds=alpha_operation_registry.DISPATCH_LEASE_SECONDS + 1
    )
    registry, record, changed, should_dispatch = (
        alpha_operation_registry.recover_after_correlation_scan_state(
            registry,
            [],
            owner_token="second",
            now=after_expiry,
            **identity,
        )
    )
    assert changed is True
    assert should_dispatch is True
    assert record["dispatch_owner_token"] == "second"
    assert record["workflow_run_id"] is None
    print("PASS alpha_registry_verified_empty_scan_allows_redispatch")


def test_provider_snapshot_audits_exact_operation_identity():
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / "snapshot.json"
        event_path = Path(tmp) / "event.json"
        snapshot_path.write_text(
            json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8"
        )
        event_path.write_text(
            json.dumps({"inputs": {"mode": "full"}}), encoding="utf-8"
        )

        workflow_sha = "e" * 40
        workflow_identity = (
            ".github/workflows/realtime-quotes.yml@" + workflow_sha
        )
        material = {"mode": "full"}
        digest = alpha_refresh_contract.canonical_command_digest(
            workflow_identity, material
        )
        values = {
            "LOOKLOOK_DISPATCH_CORRELATION_ID": "corr-audit",
            "LOOKLOOK_IDEMPOTENCY_KEY": "idem-audit",
            "LOOKLOOK_COMMAND_DIGEST": digest,
            "GITHUB_WORKFLOW_SHA": workflow_sha,
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_RUN_ID": "909",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_RUN_NUMBER": "33",
            "GITHUB_SHA": "f" * 40,
        }
        old = {name: os.environ.get(name) for name in values}
        try:
            os.environ.update(values)
            alpha_provider_contract.finalize_snapshot(snapshot_path)
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        operation = snapshot["provider_contract"]["operation"]
        assert operation["present"] is True
        assert operation["dispatch_correlation_id"] == "corr-audit"
        assert operation["workflow_identity"] == workflow_identity
        assert operation["material_execution_inputs"] == {"mode": "full"}
        assert operation["command_digest"] == digest
        assert operation["latest_run_fallback_allowed"] is False
    print("PASS alpha_provider_snapshot_operation_identity")


def test_workflows_wire_exact_claim_and_primary_manifest():
    realtime = Path(".github/workflows/realtime-quotes.yml").read_text(encoding="utf-8")
    reusable = Path(".github/workflows/reusable-a-share-quotes.yml").read_text(
        encoding="utf-8"
    )

    for token in (
        "run-name:",
        "alpha-refresh corr=",
        "dispatch_correlation_id:",
        "idempotency_key:",
        "command_digest:",
        "workflow_identity:",
        "claim-alpha-refresh:",
        "alpha_operation_registry.py claim-run",
        "GITHUB_WORKFLOW_SHA",
        "alpha-artifact-manifest.json",
    ):
        assert token in realtime, token

    claim_block = realtime.split("claim-alpha-refresh:", 1)[1].split(
        "fetch-quotes:", 1
    )[0]
    fetch_block = realtime.split("fetch-quotes:", 1)[1]
    assert "contents: write" in claim_block
    assert "contents: write" not in fetch_block
    assert "contents: read" in fetch_block
    assert "LOOKLOOK_DISPATCH_CORRELATION_ID" in fetch_block
    assert "LOOKLOOK_COMMAND_DIGEST" in fetch_block

    assert "alpha_artifact_manifest.py" in reusable
    assert ".engine/alpha-artifact-manifest.json" in reusable
    assert "LOOKLOOK_PRODUCING_COMMIT_SHA" in reusable
    assert "steps.engine-ref.outputs.ref" in reusable
    print("PASS alpha_workflow_claim_and_manifest_contract")


def main():
    tests = [
        test_capability_manifest_accepts_full_profile,
        test_capability_manifest_rejects_missing_guard,
        test_artifact_manifest_has_exact_digest_and_single_primary,
        test_artifact_manifest_allows_exact_engine_revision_override,
        test_refresh_identity_same_command_recovers_exact_run,
        test_refresh_identity_rejects_correlation_digest_conflict,
        test_refresh_identity_detects_duplicate_exact_runs,
        test_refresh_terminal_states_are_machine_distinguishable,
        test_registry_same_key_same_command_recovers_one_operation,
        test_registry_same_key_different_command_conflicts,
        test_registry_correlation_different_command_conflicts,
        test_registry_claims_exact_run_and_rejects_duplicate_dispatch,
        test_registry_distinct_commands_never_cross_wire,
        test_registry_expired_reservation_never_redispatches_without_scan,
        test_registry_correlation_scan_recovers_exact_existing_run,
        test_registry_verified_empty_scan_can_authorize_redispatch,
        test_provider_snapshot_audits_exact_operation_identity,
        test_workflows_wire_exact_claim_and_primary_manifest,
    ]
    for test in tests:
        test()
    print(f"ALPHA_PROVIDER_CONTRACT_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
