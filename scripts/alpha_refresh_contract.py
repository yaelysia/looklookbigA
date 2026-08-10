import hashlib
import json
import re


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_NAME_RE = re.compile(
    r"^alpha-refresh corr=(?P<corr>[A-Za-z0-9._:-]{1,128}) "
    r"idem=(?P<idem>[0-9a-f]{16}) cmd=(?P<cmd>[0-9a-f]{64})$"
)


class RefreshContractError(RuntimeError):
    pass


class RefreshIdentityConflict(RefreshContractError):
    pass


class RefreshOperationAmbiguous(RefreshContractError):
    pass


def validate_identifier(value, field):
    value = str(value or "")
    if not IDENTIFIER_RE.fullmatch(value):
        raise RefreshContractError(
            f"{field} must match {IDENTIFIER_RE.pattern}"
        )
    return value


def normalize_digest(value):
    value = str(value or "").lower()
    if not DIGEST_RE.fullmatch(value):
        raise RefreshContractError("command_digest must be 64 lowercase hex characters")
    return value


def canonical_command_digest(workflow_identity, material_execution_inputs):
    payload = {
        "workflow_identity": str(workflow_identity),
        "material_execution_inputs": material_execution_inputs,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def idempotency_fingerprint(idempotency_key):
    key = validate_identifier(idempotency_key, "idempotency_key")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def build_run_name(dispatch_correlation_id, idempotency_key, command_digest):
    correlation = validate_identifier(dispatch_correlation_id, "dispatch_correlation_id")
    digest = normalize_digest(command_digest)
    return (
        f"alpha-refresh corr={correlation} "
        f"idem={idempotency_fingerprint(idempotency_key)} cmd={digest}"
    )


def parse_run_name(value):
    match = RUN_NAME_RE.fullmatch(str(value or ""))
    return match.groupdict() if match else None


def operation_state(run):
    status = str(run.get("status") or "").lower()
    conclusion = str(run.get("conclusion") or "").lower()
    if status != "completed":
        if status in {"queued", "requested", "waiting", "pending"}:
            return "QUEUED"
        return "RUNNING"
    if conclusion == "success":
        return "SUCCEEDED"
    if conclusion == "cancelled":
        return "CANCELLED"
    return "FAILED"


def resolve_operation_by_correlation(
    runs,
    dispatch_correlation_id,
    idempotency_key,
    command_digest,
):
    correlation = validate_identifier(dispatch_correlation_id, "dispatch_correlation_id")
    idem = idempotency_fingerprint(idempotency_key)
    digest = normalize_digest(command_digest)

    exact = []
    correlation_conflicts = []
    idempotency_conflicts = []
    for run in runs:
        marker = parse_run_name(run.get("display_title") or run.get("run_name") or run.get("name"))
        if not marker:
            continue
        same_corr = marker["corr"] == correlation
        same_idem = marker["idem"] == idem
        same_digest = marker["cmd"] == digest
        if same_corr and same_idem and same_digest:
            exact.append(run)
        elif same_corr and not same_digest:
            correlation_conflicts.append(run)
        elif same_idem and not same_digest:
            idempotency_conflicts.append(run)

    if correlation_conflicts:
        raise RefreshIdentityConflict(
            "dispatch_correlation_id is already bound to a different command_digest"
        )
    if idempotency_conflicts:
        raise RefreshIdentityConflict(
            "idempotency_key is already bound to a different command_digest"
        )
    if len(exact) > 1:
        raise RefreshOperationAmbiguous(
            f"multiple workflow runs match one logical refresh operation: {[x.get('id') for x in exact]}"
        )
    if not exact:
        return None

    run = exact[0]
    return {
        "workflow_run_id": run.get("id"),
        "workflow_run_url": run.get("html_url") or run.get("url"),
        "head_sha": run.get("head_sha"),
        "run_attempt": run.get("run_attempt"),
        "status": operation_state(run),
        "dispatch_correlation_id": correlation,
        "idempotency_fingerprint": idem,
        "command_digest": digest,
    }
