import json
import os
from pathlib import Path

import alpha_capability_contract
import alpha_refresh_contract


CONTRACT_VERSION = "v1"


def _operation_identity_from_env():
    correlation = os.environ.get("LOOKLOOK_DISPATCH_CORRELATION_ID") or ""
    idempotency_key = os.environ.get("LOOKLOOK_IDEMPOTENCY_KEY") or ""
    command_digest = (os.environ.get("LOOKLOOK_COMMAND_DIGEST") or "").lower()
    workflow_identity = os.environ.get("LOOKLOOK_WORKFLOW_IDENTITY") or ""
    requested_mode = (os.environ.get("LOOKLOOK_REQUESTED_MODE") or "auto").lower()

    supplied = [
        bool(correlation),
        bool(idempotency_key),
        bool(command_digest),
        bool(workflow_identity),
    ]
    if any(supplied) and not all(supplied):
        raise RuntimeError(
            "refresh operation identity must provide correlation, idempotency key, command digest and workflow identity together"
        )
    if not all(supplied):
        return {
            "present": False,
            "dispatch_correlation_id": None,
            "idempotency_fingerprint": None,
            "command_digest": None,
            "workflow_identity": None,
            "material_execution_inputs": None,
        }

    alpha_refresh_contract.validate_identifier(correlation, "dispatch_correlation_id")
    alpha_refresh_contract.validate_identifier(idempotency_key, "idempotency_key")
    digest = alpha_refresh_contract.normalize_digest(command_digest)
    material_execution_inputs = {"mode": requested_mode}
    expected_digest = alpha_refresh_contract.canonical_command_digest(
        workflow_identity, material_execution_inputs
    )
    if digest != expected_digest:
        raise RuntimeError(
            f"refresh command digest mismatch: expected={expected_digest} provided={digest}"
        )

    return {
        "present": True,
        "dispatch_correlation_id": correlation,
        "idempotency_fingerprint": alpha_refresh_contract.idempotency_fingerprint(
            idempotency_key
        ),
        "command_digest": digest,
        "workflow_identity": workflow_identity,
        "material_execution_inputs": material_execution_inputs,
    }


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    manifest = alpha_capability_contract.load_manifest()
    operation = _operation_identity_from_env()

    snapshot["schema_version"] = max(int(snapshot.get("schema_version") or 0), 16)
    snapshot.setdefault("features", {})["alpha_provider_contract"] = CONTRACT_VERSION
    snapshot["provider_contract"] = {
        "name": manifest["contract_name"],
        "contract_version": manifest["contract_version"],
        "capability_manifest": "contracts/looklookalpha-provider-v1.json",
        "stable_ref": manifest["stable_ref"],
        "snapshot_schema_version": snapshot["schema_version"],
        "workflow": {
            "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF"),
            "workflow_sha": os.environ.get("GITHUB_WORKFLOW_SHA"),
            "producing_commit_sha": os.environ.get("GITHUB_SHA"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "run_number": os.environ.get("GITHUB_RUN_NUMBER"),
        },
        "operation": {
            **operation,
            "status_at_snapshot": "RUNNING" if os.environ.get("GITHUB_RUN_ID") else "LOCAL",
            "terminal_status_source": "GITHUB_ACTIONS_RUN_API",
            "latest_run_fallback_allowed": False,
        },
        "primary_artifact": {
            "name": os.environ.get(
                "LOOKLOOK_SNAPSHOT_ARTIFACT_NAME", "realtime-snapshot"
            ),
            "snapshot_path": "snapshot.json",
            "artifact_manifest_path": "alpha-artifact-manifest.json",
            "cardinality": "EXACTLY_ONE",
            "digest_algorithm": "SHA-256",
            "digest_source": "PROVIDER_COMPUTED_OR_CONSUMER_VERIFIED",
        },
    }

    alpha_capability_contract.assert_snapshot_compatible(snapshot, manifest)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "ALPHA_PROVIDER_CONTRACT "
        f"status=PASS schema_version={snapshot['schema_version']} "
        f"operation_identity={operation['present']} run_id={os.environ.get('GITHUB_RUN_ID')}"
    )
