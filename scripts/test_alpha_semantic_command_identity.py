import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import alpha_operation_registry
import alpha_provider_contract
import alpha_refresh_contract


COMMAND_SCHEMA_VERSION = "1"
EXPECTED_VECTOR = "8d623dc50e93b1735c117b004bd48e7e5e227a698aaa2c53418376789f42b9aa"
CANONICAL_BODY = {
    "execution_mode": "FULL",
    "material_provider_workflow_inputs": {
        "event_lookback_days": 30,
        "mode": "full",
    },
    "provider_workflow_identity": {
        "provider": "looklookbigA",
        "version": "v1",
        "workflow": ".github/workflows/realtime-quotes.yml",
        "workflow_sha": "a" * 40,
    },
    "requested_payload_versions": {
        "provider_contract": 1,
        "snapshot_schema": 16,
    },
    "requirements": [
        "company_events",
        "current_price_guard",
        "fundamentals",
        "intraday",
    ],
    "subjects": ["002558", "600795"],
    "temporal": {
        "fixed_cutoff": "2026-08-11T01:00:00Z",
        "mode": "AS_OF",
    },
}
CANONICAL_JCS = (
    '{"execution_mode":"FULL","material_provider_workflow_inputs":'
    '{"event_lookback_days":30,"mode":"full"},"provider_workflow_identity":'
    '{"provider":"looklookbigA","version":"v1","workflow":'
    '".github/workflows/realtime-quotes.yml","workflow_sha":'
    '"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"requested_payload_versions":'
    '{"provider_contract":1,"snapshot_schema":16},"requirements":'
    '["company_events","current_price_guard","fundamentals","intraday"],'
    '"subjects":["002558","600795"],"temporal":'
    '{"fixed_cutoff":"2026-08-11T01:00:00Z","mode":"AS_OF"}}'
)


def semantic_digest(body=None, schema_version=COMMAND_SCHEMA_VERSION):
    return alpha_refresh_contract.canonical_command_digest(
        schema_version,
        CANONICAL_BODY if body is None else body,
    )


def test_domain_separated_digest_vector():
    assert alpha_refresh_contract.SEMANTIC_COMMAND_DOMAIN == (
        "looklookAlpha.refresh-command.v1"
    )
    assert alpha_refresh_contract.digest_from_canonical_jcs(
        COMMAND_SCHEMA_VERSION, CANONICAL_JCS
    ) == EXPECTED_VECTOR
    # This fixture only uses JSON values whose sorted compact encoding is the
    # same byte representation as RFC8785/JCS. Alpha remains authoritative for
    # general JCS canonicalization in production.
    assert semantic_digest() == EXPECTED_VECTOR
    print("PASS alpha_semantic_digest_domain_vector")


def test_dispatch_metadata_does_not_change_semantic_digest():
    first_dispatch = {
        "dispatch_correlation_id": "corr-a",
        "idempotency_key": "idem-a",
        "deadline": "2026-08-11T02:00:00Z",
        "submitted_time": "2026-08-11T01:00:00Z",
        "caller_local_job_id": "job-a",
        "retry_attempt": 0,
        "local_persistence_object_id": "row-a",
        "local_persistence_revision": 1,
        "local_persistence_created_at": "2026-08-11T01:00:00Z",
    }
    second_dispatch = {
        "dispatch_correlation_id": "corr-b",
        "idempotency_key": "idem-b",
        "deadline": "2026-08-11T03:00:00Z",
        "submitted_time": "2026-08-11T01:05:00Z",
        "caller_local_job_id": "job-b",
        "retry_attempt": 7,
        "local_persistence_object_id": "row-b",
        "local_persistence_revision": 9,
        "local_persistence_created_at": "2026-08-11T01:05:00Z",
    }
    assert first_dispatch != second_dispatch
    # Dispatch metadata is intentionally not an argument to semantic_digest.
    assert semantic_digest() == EXPECTED_VECTOR
    assert semantic_digest(copy.deepcopy(CANONICAL_BODY)) == EXPECTED_VECTOR
    print("PASS alpha_dispatch_metadata_excluded_from_semantic_digest")


def test_every_material_dimension_changes_semantic_digest():
    cases = []

    body = copy.deepcopy(CANONICAL_BODY)
    body["provider_workflow_identity"]["workflow_sha"] = "b" * 40
    cases.append(("workflow_sha", body))

    body = copy.deepcopy(CANONICAL_BODY)
    body["requirements"] = body["requirements"] + ["market_environment"]
    cases.append(("requirements", body))

    body = copy.deepcopy(CANONICAL_BODY)
    body["subjects"] = ["002558"]
    cases.append(("subjects", body))

    body = copy.deepcopy(CANONICAL_BODY)
    body["temporal"]["fixed_cutoff"] = "2026-08-11T01:01:00Z"
    cases.append(("temporal", body))

    body = copy.deepcopy(CANONICAL_BODY)
    body["execution_mode"] = "INTRADAY_FAST"
    cases.append(("execution_mode", body))

    body = copy.deepcopy(CANONICAL_BODY)
    body["material_provider_workflow_inputs"]["event_lookback_days"] = 90
    cases.append(("material_provider_workflow_inputs", body))

    body = copy.deepcopy(CANONICAL_BODY)
    body["requested_payload_versions"]["snapshot_schema"] = 17
    cases.append(("requested_payload_versions", body))

    base = semantic_digest()
    for field, changed in cases:
        assert semantic_digest(changed) != base, field
    assert semantic_digest(schema_version="2") != base
    print("PASS alpha_material_semantics_change_digest")


def test_registry_preserves_caller_digest_and_fails_closed_on_mismatch():
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    registry = alpha_operation_registry.empty_registry()
    workflow_identity = ".github/workflows/realtime-quotes.yml@" + "a" * 40
    digest = EXPECTED_VECTOR
    identity = {
        "dispatch_correlation_id": "corr-semantic-a",
        "idempotency_key": "idem-semantic",
        "command_digest": digest,
        "workflow_identity": workflow_identity,
        "material_execution_inputs": {"mode": "full"},
    }
    registry, first, changed, should_dispatch = alpha_operation_registry.reserve_state(
        registry, owner_token="owner-a", now=now, **identity
    )
    assert changed and should_dispatch
    assert first["command_digest"] == digest
    assert first["command_digest_source"] == "CALLER_SUPPLIED_SEMANTIC"

    alias = dict(identity)
    alias["dispatch_correlation_id"] = "corr-semantic-b"
    registry, recovered, changed, should_dispatch = (
        alpha_operation_registry.reserve_state(
            registry, owner_token="owner-b", now=now, **alias
        )
    )
    assert recovered["operation_id"] == first["operation_id"]
    assert changed is True and should_dispatch is False

    different_semantics = dict(identity)
    different_semantics["dispatch_correlation_id"] = "corr-semantic-c"
    different_semantics["command_digest"] = "b" * 64
    try:
        alpha_operation_registry.reserve_state(
            registry, owner_token="owner-c", now=now, **different_semantics
        )
    except alpha_refresh_contract.RefreshIdentityConflict:
        pass
    else:
        raise AssertionError("same idempotency key with different semantic digest must conflict")

    contradictory_material = dict(identity)
    contradictory_material["dispatch_correlation_id"] = "corr-semantic-d"
    contradictory_material["material_execution_inputs"] = {"mode": "intraday_fast"}
    try:
        alpha_operation_registry.reserve_state(
            registry, owner_token="owner-d", now=now, **contradictory_material
        )
    except alpha_refresh_contract.RefreshIdentityConflict:
        pass
    else:
        raise AssertionError(
            "same digest with contradictory provider-visible material inputs must fail closed"
        )
    print("PASS alpha_registry_preserves_semantic_digest")


def test_snapshot_preserves_caller_semantic_digest():
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / "snapshot.json"
        event_path = Path(tmp) / "event.json"
        snapshot_path.write_text(
            Path("fixtures/looklookalpha/provider-v1-snapshot.json").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        event_path.write_text(
            json.dumps({"inputs": {"mode": "full"}}), encoding="utf-8"
        )
        workflow_sha = "a" * 40
        values = {
            "LOOKLOOK_DISPATCH_CORRELATION_ID": "corr-snapshot",
            "LOOKLOOK_IDEMPOTENCY_KEY": "idem-snapshot",
            "LOOKLOOK_COMMAND_DIGEST": EXPECTED_VECTOR,
            "GITHUB_WORKFLOW_SHA": workflow_sha,
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_RUN_ID": "901",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_RUN_NUMBER": "12",
            "GITHUB_SHA": "c" * 40,
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
        operation = json.loads(snapshot_path.read_text(encoding="utf-8"))[
            "provider_contract"
        ]["operation"]
        assert operation["command_digest"] == EXPECTED_VECTOR
        assert operation["command_digest_source"] == "CALLER_SUPPLIED_SEMANTIC"
        assert operation["material_execution_inputs"] == {"mode": "full"}
    print("PASS alpha_snapshot_preserves_semantic_digest")


def test_machine_contract_and_workflow_do_not_redefine_digest():
    contract = json.loads(
        Path("contracts/looklookalpha-provider-v1.json").read_text(encoding="utf-8")
    )
    semantic = contract["refresh_operation"]["semantic_command_digest"]
    assert semantic["domain"] == "looklookAlpha.refresh-command.v1"
    assert semantic["canonicalization"] == "RFC8785_JCS"
    assert semantic["authority"] == "CALLER"
    assert semantic["provider_behavior"] == "VALIDATE_64_HEX_AND_PRESERVE"
    assert "dispatch_correlation_id" in semantic["excluded_dispatch_fields"]
    assert "idempotency_key" in semantic["excluded_dispatch_fields"]
    assert set(semantic["canonical_body_fields"]) >= {
        "provider_workflow_identity",
        "requirements",
        "subjects",
        "temporal",
        "execution_mode",
        "material_provider_workflow_inputs",
        "requested_payload_versions",
    }

    realtime = Path(".github/workflows/realtime-quotes.yml").read_text(encoding="utf-8")
    claim = realtime.split("claim-alpha-refresh:", 1)[1].split("fetch-quotes:", 1)[0]
    assert "normalize_digest" in claim
    assert "expected_digest" not in claim
    assert "canonical_command_digest(" not in claim
    print("PASS alpha_provider_does_not_redefine_semantic_digest")


def main():
    tests = [
        test_domain_separated_digest_vector,
        test_dispatch_metadata_does_not_change_semantic_digest,
        test_every_material_dimension_changes_semantic_digest,
        test_registry_preserves_caller_digest_and_fails_closed_on_mismatch,
        test_snapshot_preserves_caller_semantic_digest,
        test_machine_contract_and_workflow_do_not_redefine_digest,
    ]
    for test in tests:
        test()
    print(f"ALPHA_SEMANTIC_COMMAND_IDENTITY_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
