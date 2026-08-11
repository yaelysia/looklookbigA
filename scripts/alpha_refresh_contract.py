import hashlib
import json
import re


SEMANTIC_COMMAND_DOMAIN = "looklookAlpha.refresh-command.v1"
OPERATION_IDENTITY_DOMAIN = "looklookbigA.refresh-operation-identity.v1"
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_NAME_RE = re.compile(
    r"^alpha-refresh corr=(?P<corr>[A-Za-z0-9._:-]{1,128}) "
    r"op=(?P<op>[0-9a-f]{64}) cmd=(?P<cmd>[0-9a-f]{64})$"
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


def digest_from_canonical_jcs(command_schema_version, canonical_body_jcs):
    """Reference digest for already-RFC8785-canonicalized command bodies.

    looklookbigA does not need to implement JCS for production dispatch. Alpha may
    compute the semantic digest and supply it. This helper exists for fixtures
    and cross-contract test vectors only.
    """
    version = str(command_schema_version or "")
    if not version:
        raise RefreshContractError("command_schema_version is required")
    if isinstance(canonical_body_jcs, str):
        body = canonical_body_jcs.encode("utf-8")
    elif isinstance(canonical_body_jcs, (bytes, bytearray)):
        body = bytes(canonical_body_jcs)
    else:
        raise RefreshContractError("canonical_body_jcs must be UTF-8 text or bytes")
    payload = (
        SEMANTIC_COMMAND_DOMAIN.encode("utf-8")
        + b"\x00"
        + version.encode("utf-8")
        + b"\x00"
        + body
    )
    return hashlib.sha256(payload).hexdigest()


def canonical_command_digest(command_schema_version, canonical_command_body):
    """Deterministic fixture helper.

    Production provider paths treat command_digest as caller-supplied semantic
    identity and never recompute it from the limited workflow inputs visible to
    looklookbigA. For simple JSON fixtures this helper serializes deterministically
    and applies the domain-separated digest formula; Alpha remains authoritative
    for RFC8785/JCS canonicalization.
    """
    canonical = json.dumps(
        canonical_command_body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return digest_from_canonical_jcs(command_schema_version, canonical)


def idempotency_fingerprint(idempotency_key):
    key = validate_identifier(idempotency_key, "idempotency_key")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def operation_identity_fingerprint(
    idempotency_key,
    command_digest,
    workflow_identity,
    material_execution_inputs,
):
    """Hash the provider-visible dispatch identity without exposing the key.

    This is intentionally distinct from the Alpha semantic command digest. It
    exists only so crash recovery can prove that a candidate workflow run used
    the same idempotency identity, semantic digest, exact workflow identity and
    material provider-visible inputs as the durable reservation.
    """
    if not isinstance(material_execution_inputs, dict):
        raise RefreshContractError("material_execution_inputs must be a JSON object")
    payload = {
        "idempotency_fingerprint": idempotency_fingerprint(idempotency_key),
        "command_digest": normalize_digest(command_digest),
        "workflow_identity": str(workflow_identity or ""),
        "material_execution_inputs": material_execution_inputs,
    }
    if not payload["workflow_identity"]:
        raise RefreshContractError("workflow_identity is required")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(
        OPERATION_IDENTITY_DOMAIN.encode("utf-8") + b"\x00" + encoded
    ).hexdigest()


def build_run_name(dispatch_correlation_id, operation_fingerprint, command_digest):
    correlation = validate_identifier(dispatch_correlation_id, "dispatch_correlation_id")
    operation_fingerprint = normalize_digest(operation_fingerprint)
    digest = normalize_digest(command_digest)
    return f"alpha-refresh corr={correlation} op={operation_fingerprint} cmd={digest}"


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


def _workflow_sha(workflow_identity):
    value = str(workflow_identity or "")
    if "@" not in value:
        raise RefreshContractError("workflow_identity must include an exact @<sha> suffix")
    sha = value.rsplit("@", 1)[1].lower()
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise RefreshContractError("workflow_identity must end with an exact 40-hex SHA")
    return sha


def verify_scanned_runs(
    runs,
    correlation_ids,
    idempotency_key,
    command_digest,
    workflow_identity,
    material_execution_inputs,
):
    """Attach proof that matching scan candidates have the full operation identity.

    Recovery must never bind a raw workflow run selected only by correlation and
    semantic digest. A candidate is admitted only when its run-name operation
    fingerprint and head SHA prove the durable reservation's complete
    provider-visible dispatch identity.
    """
    aliases = {
        validate_identifier(value, "dispatch_correlation_id")
        for value in (correlation_ids or [])
    }
    digest = normalize_digest(command_digest)
    idem = idempotency_fingerprint(idempotency_key)
    operation_fingerprint = operation_identity_fingerprint(
        idempotency_key,
        digest,
        workflow_identity,
        material_execution_inputs,
    )
    workflow_sha = _workflow_sha(workflow_identity)
    verified = []
    conflicts = []

    for run in runs:
        marker = parse_run_name(
            run.get("display_title") or run.get("run_name") or run.get("name")
        )
        if not marker or marker["corr"] not in aliases:
            continue
        if (
            marker["cmd"] != digest
            or marker["op"] != operation_fingerprint
            or str(run.get("head_sha") or "").lower() != workflow_sha
        ):
            conflicts.append(run)
            continue
        enriched = dict(run)
        enriched["_looklook_verified_operation_identity"] = {
            "verified": True,
            "idempotency_fingerprint": idem,
            "operation_identity_fingerprint": operation_fingerprint,
            "command_digest": digest,
            "workflow_identity": workflow_identity,
            "material_execution_inputs": material_execution_inputs,
        }
        verified.append(enriched)

    if conflicts:
        raise RefreshIdentityConflict(
            "correlation scan found a run with contradictory provider operation identity"
        )
    return verified


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
    for run in runs:
        marker = parse_run_name(
            run.get("display_title") or run.get("run_name") or run.get("name")
        )
        if not marker:
            continue
        same_corr = marker["corr"] == correlation
        same_digest = marker["cmd"] == digest
        evidence = run.get("_looklook_verified_operation_identity") or {}
        verified_identity = (
            evidence.get("verified") is True
            and evidence.get("idempotency_fingerprint") == idem
            and evidence.get("command_digest") == digest
            and evidence.get("operation_identity_fingerprint") == marker["op"]
        )
        if same_corr and same_digest and verified_identity:
            exact.append(run)
        elif same_corr:
            correlation_conflicts.append(run)

    if correlation_conflicts:
        raise RefreshIdentityConflict(
            "dispatch_correlation_id matched an unverified or contradictory workflow run"
        )
    if len(exact) > 1:
        raise RefreshOperationAmbiguous(
            f"multiple workflow runs match one logical refresh operation: {[x.get('id') for x in exact]}"
        )
    if not exact:
        return None

    run = exact[0]
    evidence = run["_looklook_verified_operation_identity"]
    return {
        "workflow_run_id": run.get("id"),
        "workflow_run_url": run.get("html_url") or run.get("url"),
        "head_sha": run.get("head_sha"),
        "run_attempt": run.get("run_attempt"),
        "status": operation_state(run),
        "dispatch_correlation_id": correlation,
        "idempotency_fingerprint": idem,
        "operation_identity_fingerprint": evidence["operation_identity_fingerprint"],
        "command_digest": digest,
        "workflow_identity": evidence["workflow_identity"],
        "material_execution_inputs": evidence["material_execution_inputs"],
    }
