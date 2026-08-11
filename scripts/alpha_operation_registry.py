import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import alpha_refresh_contract


REGISTRY_BRANCH = "provider-operations"
REGISTRY_PATH = ".provider-operations/refresh-registry-v1.json"
REGISTRY_SCHEMA = 1
DISPATCH_LEASE_SECONDS = 1800
MAX_CAS_ATTEMPTS = 8


class RegistryError(RuntimeError):
    pass


class RegistryConflict(RegistryError):
    pass


class RegistryTransportError(RegistryError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def empty_registry():
    return {
        "schema_version": REGISTRY_SCHEMA,
        "kind": "looklookbigA-alpha-refresh-registry",
        "updated_at": None,
        "operations": {},
    }


def _operation_id(idem_fp, command_digest, workflow_identity):
    import hashlib

    payload = f"{idem_fp}\n{command_digest}\n{workflow_identity}".encode("utf-8")
    return "op_" + hashlib.sha256(payload).hexdigest()[:32]


def _normalize_material_inputs(value):
    if not isinstance(value, dict):
        raise RegistryError("material execution inputs must be a JSON object")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RegistryError(f"material execution inputs are not JSON-safe: {exc}") from exc
    return json.loads(encoded)


def _validate_command(workflow_identity, material_execution_inputs, command_digest):
    """Validate transport shape without redefining Alpha semantic identity.

    Alpha is authoritative for the RFC8785/JCS semantic command digest. The
    provider only validates the supplied 64-hex digest, preserves it unchanged,
    and separately audits the exact workflow identity/material execution inputs.
    """
    if not str(workflow_identity or ""):
        raise RegistryError("workflow_identity is required")
    _normalize_material_inputs(material_execution_inputs)
    return alpha_refresh_contract.normalize_digest(command_digest)


def _find_by_correlation(registry, correlation):
    for record in (registry.get("operations") or {}).values():
        if correlation in (record.get("correlation_ids") or []):
            return record
    return None


def _find_by_idempotency(registry, idem_fp):
    for record in (registry.get("operations") or {}).values():
        if record.get("idempotency_fingerprint") == idem_fp:
            return record
    return None


def _record_matches(
    record,
    idem_fp,
    digest,
    workflow_identity,
    material_execution_inputs,
):
    return (
        record.get("idempotency_fingerprint") == idem_fp
        and record.get("command_digest") == digest
        and record.get("workflow_identity") == workflow_identity
        and record.get("material_execution_inputs") == material_execution_inputs
    )


def _identity_values(
    dispatch_correlation_id,
    idempotency_key,
    command_digest,
    workflow_identity,
    material_execution_inputs,
):
    correlation = alpha_refresh_contract.validate_identifier(
        dispatch_correlation_id, "dispatch_correlation_id"
    )
    alpha_refresh_contract.validate_identifier(idempotency_key, "idempotency_key")
    idem_fp = alpha_refresh_contract.idempotency_fingerprint(idempotency_key)
    material = _normalize_material_inputs(material_execution_inputs)
    workflow = str(workflow_identity or "")
    digest = _validate_command(workflow, material, command_digest)
    return correlation, idem_fp, digest, workflow, material


def reserve_state(
    registry,
    *,
    dispatch_correlation_id,
    idempotency_key,
    command_digest,
    workflow_identity,
    material_execution_inputs,
    owner_token=None,
    now=None,
):
    """Reserve a new command or recover an existing one.

    command_digest is caller-supplied semantic identity. Dispatch metadata is
    deliberately not folded into it. Existing operations whose dispatch lease
    expired are not automatically redispatched: callers must first perform a
    fresh correlation scan.
    """
    now = now or utc_now()
    correlation, idem_fp, digest, workflow, material = _identity_values(
        dispatch_correlation_id,
        idempotency_key,
        command_digest,
        workflow_identity,
        material_execution_inputs,
    )
    owner_token = owner_token or str(uuid.uuid4())

    result = deepcopy(registry or empty_registry())
    operations = result.setdefault("operations", {})

    by_corr = _find_by_correlation(result, correlation)
    if by_corr and not _record_matches(
        by_corr, idem_fp, digest, workflow, material
    ):
        raise alpha_refresh_contract.RefreshIdentityConflict(
            "dispatch_correlation_id is already bound to a different logical command"
        )

    by_key = _find_by_idempotency(result, idem_fp)
    if by_key and not _record_matches(
        by_key, idem_fp, digest, workflow, material
    ):
        raise alpha_refresh_contract.RefreshIdentityConflict(
            "idempotency_key is already bound to a different logical command"
        )

    existing = by_corr or by_key
    changed = False
    if existing:
        if correlation not in existing.setdefault("correlation_ids", []):
            existing["correlation_ids"].append(correlation)
            existing["correlation_ids"].sort()
            existing["updated_at"] = iso(now)
            result["updated_at"] = iso(now)
            changed = True

        if existing.get("workflow_run_id"):
            return result, deepcopy(existing), changed, False

        lease_until = parse_iso(existing.get("dispatch_lease_until"))
        if lease_until and lease_until > now:
            return result, deepcopy(existing), changed, False

        return result, deepcopy(existing), changed, False

    operation_id = _operation_id(idem_fp, digest, workflow)
    record = {
        "operation_id": operation_id,
        "primary_correlation_id": correlation,
        "correlation_ids": [correlation],
        "idempotency_fingerprint": idem_fp,
        "command_digest": digest,
        "command_digest_source": "CALLER_SUPPLIED_SEMANTIC",
        "workflow_identity": workflow,
        "material_execution_inputs": material,
        "dispatch_state": "RESERVED",
        "dispatch_owner_token": owner_token,
        "dispatch_lease_until": iso(
            now + timedelta(seconds=DISPATCH_LEASE_SECONDS)
        ),
        "workflow_run_id": None,
        "workflow_run_url": None,
        "workflow_head_sha": None,
        "workflow_run_attempt": None,
        "created_at": iso(now),
        "updated_at": iso(now),
    }
    operations[operation_id] = record
    result["updated_at"] = iso(now)
    return result, deepcopy(record), True, True


def claim_run_state(
    registry,
    *,
    dispatch_correlation_id,
    idempotency_key,
    command_digest,
    workflow_identity,
    material_execution_inputs,
    workflow_run_id,
    workflow_run_url=None,
    workflow_head_sha=None,
    workflow_run_attempt=None,
    now=None,
):
    now = now or utc_now()
    correlation, idem_fp, digest, workflow, material = _identity_values(
        dispatch_correlation_id,
        idempotency_key,
        command_digest,
        workflow_identity,
        material_execution_inputs,
    )
    result = deepcopy(registry or empty_registry())
    record = _find_by_correlation(result, correlation)
    if not record:
        raise RegistryConflict(
            "workflow started without a durable pre-dispatch reservation"
        )
    if not _record_matches(record, idem_fp, digest, workflow, material):
        raise alpha_refresh_contract.RefreshIdentityConflict(
            "workflow run identity does not match the durable reservation"
        )

    run_id = int(workflow_run_id)
    existing_run_id = record.get("workflow_run_id")
    if existing_run_id is not None and int(existing_run_id) != run_id:
        raise RegistryConflict(
            f"duplicate workflow runs detected for one logical operation: "
            f"existing={existing_run_id} new={run_id}"
        )

    record["workflow_run_id"] = run_id
    record["workflow_run_url"] = workflow_run_url
    record["workflow_head_sha"] = workflow_head_sha
    record["workflow_run_attempt"] = (
        int(workflow_run_attempt)
        if workflow_run_attempt not in (None, "")
        else None
    )
    record["dispatch_state"] = "DISPATCHED"
    record["dispatch_owner_token"] = None
    record["dispatch_lease_until"] = None
    record["updated_at"] = iso(now)
    result["updated_at"] = iso(now)
    return result, deepcopy(record), True


def resolve_state(
    registry,
    *,
    dispatch_correlation_id,
    idempotency_key,
    command_digest,
    workflow_identity,
    material_execution_inputs,
):
    correlation, idem_fp, digest, workflow, material = _identity_values(
        dispatch_correlation_id,
        idempotency_key,
        command_digest,
        workflow_identity,
        material_execution_inputs,
    )
    record = _find_by_correlation(registry or empty_registry(), correlation)
    if not record:
        return None
    if not _record_matches(record, idem_fp, digest, workflow, material):
        raise alpha_refresh_contract.RefreshIdentityConflict(
            "correlation resolves to a different logical command"
        )
    return deepcopy(record)


def recover_after_correlation_scan_state(
    registry,
    runs,
    *,
    dispatch_correlation_id,
    idempotency_key,
    command_digest,
    workflow_identity,
    material_execution_inputs,
    owner_token=None,
    now=None,
):
    """Recover an exact run or authorize redispatch only after a fresh scan."""
    now = now or utc_now()
    correlation, idem_fp, digest, workflow, material = _identity_values(
        dispatch_correlation_id,
        idempotency_key,
        command_digest,
        workflow_identity,
        material_execution_inputs,
    )
    result = deepcopy(registry or empty_registry())
    record = _find_by_correlation(result, correlation) or _find_by_idempotency(
        result, idem_fp
    )
    if not record:
        raise RegistryConflict("cannot recover an operation that was never reserved")
    if not _record_matches(record, idem_fp, digest, workflow, material):
        raise alpha_refresh_contract.RefreshIdentityConflict(
            "recovery identity does not match the durable reservation"
        )
    if record.get("workflow_run_id"):
        return result, deepcopy(record), False, False

    matches = {}
    for alias in record.get("correlation_ids") or []:
        found = alpha_refresh_contract.resolve_operation_by_correlation(
            runs, alias, idempotency_key, digest
        )
        if found:
            matches[int(found["workflow_run_id"])] = (alias, found)
    if len(matches) > 1:
        raise alpha_refresh_contract.RefreshOperationAmbiguous(
            f"multiple workflow runs found during correlation recovery: "
            f"{sorted(matches)}"
        )
    if matches:
        alias, found = next(iter(matches.values()))
        claimed = claim_run_state(
            result,
            dispatch_correlation_id=alias,
            idempotency_key=idempotency_key,
            command_digest=digest,
            workflow_identity=workflow,
            material_execution_inputs=material,
            workflow_run_id=found["workflow_run_id"],
            workflow_run_url=found.get("workflow_run_url"),
            workflow_head_sha=found.get("head_sha"),
            workflow_run_attempt=found.get("run_attempt"),
            now=now,
        )
        return (*claimed, False)

    lease_until = parse_iso(record.get("dispatch_lease_until"))
    if lease_until and lease_until > now:
        return result, deepcopy(record), False, False

    record["dispatch_owner_token"] = owner_token or str(uuid.uuid4())
    record["dispatch_lease_until"] = iso(
        now + timedelta(seconds=DISPATCH_LEASE_SECONDS)
    )
    record["dispatch_state"] = "RESERVED"
    record["updated_at"] = iso(now)
    result["updated_at"] = iso(now)
    return result, deepcopy(record), True, True


class GitHubRegistryStore:
    def __init__(self, repository, token, branch=REGISTRY_BRANCH, path=REGISTRY_PATH):
        self.repository = repository
        self.token = token
        self.branch = branch
        self.path = path
        self.api = f"https://api.github.com/repos/{repository}"

    def _request(self, method, url, payload=None, allow_404=False):
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "looklookbigA-alpha-refresh-contract",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if allow_404 and exc.code == 404:
                return 404, None
            if exc.code in {409, 422}:
                raise RegistryConflict(
                    f"GitHub CAS conflict HTTP {exc.code}: {raw[:500]}"
                ) from exc
            raise RegistryTransportError(
                f"GitHub API HTTP {exc.code}: {raw[:500]}"
            ) from exc
        except Exception as exc:
            raise RegistryTransportError(f"GitHub API request failed: {exc}") from exc

    def ensure_branch(self, base_sha):
        quoted = urllib.parse.quote(f"heads/{self.branch}", safe="/")
        status, _ = self._request(
            "GET", f"{self.api}/git/ref/{quoted}", allow_404=True
        )
        if status != 404:
            return
        try:
            self._request(
                "POST",
                f"{self.api}/git/refs",
                {"ref": f"refs/heads/{self.branch}", "sha": base_sha},
            )
        except RegistryConflict:
            status, _ = self._request(
                "GET", f"{self.api}/git/ref/{quoted}", allow_404=True
            )
            if status == 404:
                raise

    def load(self):
        encoded_path = urllib.parse.quote(self.path, safe="/")
        query = urllib.parse.urlencode({"ref": self.branch})
        status, obj = self._request(
            "GET", f"{self.api}/contents/{encoded_path}?{query}", allow_404=True
        )
        if status == 404:
            return empty_registry(), None
        content = base64.b64decode(obj["content"]).decode("utf-8")
        return json.loads(content), obj["sha"]

    def save(self, registry, sha):
        encoded_path = urllib.parse.quote(self.path, safe="/")
        payload = {
            "message": "Update Alpha refresh operation registry",
            "content": base64.b64encode(
                (
                    json.dumps(registry, ensure_ascii=False, indent=2) + "\n"
                ).encode("utf-8")
            ).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha
        _, obj = self._request(
            "PUT", f"{self.api}/contents/{encoded_path}", payload
        )
        return (obj.get("content") or {}).get("sha")

    def mutate(self, mutator):
        for attempt in range(MAX_CAS_ATTEMPTS):
            registry, sha = self.load()
            new_registry, record, changed, flag = mutator(registry)
            if not changed:
                return record, flag
            try:
                self.save(new_registry, sha)
                return record, flag
            except RegistryConflict:
                if attempt + 1 >= MAX_CAS_ATTEMPTS:
                    raise
                time.sleep(0.05 * (attempt + 1))
        raise RegistryConflict("registry CAS retry budget exhausted")


def reserve_or_recover(store, **kwargs):
    return store.mutate(lambda registry: reserve_state(registry, **kwargs))


def claim_run(store, **kwargs):
    record, _ = store.mutate(
        lambda registry: (*claim_run_state(registry, **kwargs), False)
    )
    return record


def recover_after_correlation_scan(store, runs, **kwargs):
    return store.mutate(
        lambda registry: recover_after_correlation_scan_state(
            registry, runs, **kwargs
        )
    )


def _parse_material_inputs(raw):
    value = json.loads(raw)
    return _normalize_material_inputs(value)


def _store_from_args(args):
    token = os.environ.get(args.token_env)
    if not token:
        raise RegistryError(f"missing token in environment variable {args.token_env}")
    return GitHubRegistryStore(args.repository, token, args.registry_branch)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Durable Alpha refresh operation registry"
    )
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--registry-branch", default=REGISTRY_BRANCH)
    sub = parser.add_subparsers(dest="command", required=True)

    def identity_args(p):
        p.add_argument("--correlation", required=True)
        p.add_argument("--idempotency-key", required=True)
        p.add_argument("--command-digest", required=True)
        p.add_argument("--workflow-identity", required=True)
        p.add_argument("--material-inputs-json", required=True)

    reserve = sub.add_parser("reserve")
    identity_args(reserve)
    reserve.add_argument("--base-sha", required=True)
    reserve.add_argument("--owner-token", default=None)

    claim = sub.add_parser("claim-run")
    identity_args(claim)
    claim.add_argument("--base-sha", required=True)
    claim.add_argument("--run-id", required=True, type=int)
    claim.add_argument("--run-url", default=None)
    claim.add_argument("--head-sha", default=None)
    claim.add_argument("--run-attempt", default=None)

    resolve = sub.add_parser("resolve")
    identity_args(resolve)

    args = parser.parse_args(argv)
    if not args.repository:
        raise RegistryError("repository is required")
    material = _parse_material_inputs(args.material_inputs_json)
    store = _store_from_args(args)

    if args.command in {"reserve", "claim-run"}:
        store.ensure_branch(args.base_sha)

    identity = {
        "dispatch_correlation_id": args.correlation,
        "idempotency_key": args.idempotency_key,
        "command_digest": args.command_digest,
        "workflow_identity": args.workflow_identity,
        "material_execution_inputs": material,
    }
    if args.command == "reserve":
        record, should_dispatch = reserve_or_recover(
            store, owner_token=args.owner_token, **identity
        )
        print(
            json.dumps(
                {"operation": record, "should_dispatch": should_dispatch}
            )
        )
    elif args.command == "claim-run":
        record = claim_run(
            store,
            workflow_run_id=args.run_id,
            workflow_run_url=args.run_url,
            workflow_head_sha=args.head_sha,
            workflow_run_attempt=args.run_attempt,
            **identity,
        )
        print(json.dumps({"operation": record}))
    else:
        registry, _ = store.load()
        record = resolve_state(registry, **identity)
        print(json.dumps({"operation": record}))


if __name__ == "__main__":
    main()
