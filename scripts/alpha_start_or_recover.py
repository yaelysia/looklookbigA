import json
import os
import urllib.parse
import urllib.request
from datetime import timedelta

import alpha_operation_registry
import alpha_refresh_contract


DEFAULT_WORKFLOW = ".github/workflows/realtime-quotes.yml"
MAX_SCAN_PAGES = 100
EXTERNAL_OPERATION_STATES = (
    "ACCEPTED",
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
)


class StartOrRecoverError(RuntimeError):
    pass


def _request_json(url, token):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "looklookbigA-alpha-start-or-recover",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_workflow_run(repository, token, workflow_run_id):
    try:
        run_id = int(workflow_run_id)
    except (TypeError, ValueError) as exc:
        raise StartOrRecoverError("workflow_run_id must be an integer") from exc
    if run_id <= 0:
        raise StartOrRecoverError("workflow_run_id must be positive")
    run = _request_json(
        f"https://api.github.com/repos/{repository}/actions/runs/{run_id}", token
    )
    try:
        returned_id = int(run.get("id"))
    except (TypeError, ValueError) as exc:
        raise StartOrRecoverError("exact workflow run response is missing its id") from exc
    if returned_id != run_id:
        raise StartOrRecoverError(
            f"exact workflow run lookup returned id={returned_id}, expected={run_id}"
        )
    return run


def external_operation_state(record, run=None):
    """Map one durable provider operation to the stable external state contract.

    Before GitHub has an exact run id, a valid durable reservation is ACCEPTED.
    Once a run id exists, state must come from that exact GitHub Actions run;
    callers are never allowed to substitute a latest/other run.
    """
    workflow_run_id = record.get("workflow_run_id")
    if workflow_run_id is None:
        if run is not None:
            raise StartOrRecoverError(
                "workflow run state cannot be attached before an exact run is bound"
            )
        if record.get("dispatch_state") != "RESERVED":
            raise StartOrRecoverError(
                "operation without an exact run must be a durable RESERVED operation"
            )
        return "ACCEPTED", "DURABLE_RESERVATION"

    if run is None:
        raise StartOrRecoverError(
            "exact GitHub Actions run payload is required once workflow_run_id is bound"
        )
    try:
        expected_id = int(workflow_run_id)
        actual_id = int(run.get("id"))
    except (TypeError, ValueError) as exc:
        raise StartOrRecoverError("workflow run identity is not machine-valid") from exc
    if actual_id != expected_id:
        raise StartOrRecoverError(
            f"workflow run state cross-wire: expected id={expected_id}, got id={actual_id}"
        )

    state = alpha_refresh_contract.operation_state(run)
    if state not in EXTERNAL_OPERATION_STATES or state == "ACCEPTED":
        raise StartOrRecoverError(f"unsupported external operation state: {state}")
    return state, "GITHUB_ACTIONS_RUN_API"


def scan_workflow_runs(repository, token, workflow_path, not_before=None):
    """Return a fresh workflow-dispatch run scan, never a guessed latest run.

    GitHub returns runs newest first. Once a full page is older than the
    operation reservation window we can stop; every plausible run created
    after the durable reservation has already been inspected by correlation.
    """
    workflow_id = str(workflow_path).rsplit("/", 1)[-1]
    encoded_workflow = urllib.parse.quote(workflow_id, safe="")
    api = f"https://api.github.com/repos/{repository}/actions/workflows/{encoded_workflow}/runs"
    floor = None
    if not_before:
        floor = alpha_operation_registry.parse_iso(not_before) - timedelta(minutes=5)

    runs = []
    for page in range(1, MAX_SCAN_PAGES + 1):
        query = urllib.parse.urlencode(
            {"event": "workflow_dispatch", "per_page": 100, "page": page}
        )
        payload = _request_json(f"{api}?{query}", token)
        batch = payload.get("workflow_runs") or []
        runs.extend(batch)
        if len(batch) < 100:
            break
        if floor:
            created = [
                alpha_operation_registry.parse_iso(run.get("created_at"))
                for run in batch
                if run.get("created_at")
            ]
            if created and max(created) < floor:
                break
    else:
        raise StartOrRecoverError(
            f"workflow run scan exceeded {MAX_SCAN_PAGES} pages; refusing to guess"
        )
    return runs


def _operation_fingerprint(identity):
    return alpha_refresh_contract.operation_identity_fingerprint(
        identity["idempotency_key"],
        identity["command_digest"],
        identity["workflow_identity"],
        identity["material_execution_inputs"],
    )


def _verified_run_for_record(record, verified_runs):
    try:
        expected_id = int(record["workflow_run_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StartOrRecoverError(
            "recovered operation is missing its exact workflow_run_id"
        ) from exc
    matches = []
    for run in verified_runs:
        try:
            run_id = int(run.get("id"))
        except (TypeError, ValueError):
            continue
        if run_id == expected_id:
            matches.append(run)
    if len(matches) != 1:
        raise StartOrRecoverError(
            f"recovered operation expected one verified run id={expected_id}, got {len(matches)}"
        )
    return matches[0]


def _expose_dispatch_identity(record, identity, *, repository, token, run=None):
    exposed = dict(record)
    exposed["operation_identity_fingerprint"] = _operation_fingerprint(identity)
    if exposed.get("workflow_run_id") is not None and run is None:
        run = fetch_workflow_run(repository, token, exposed["workflow_run_id"])
    state, source = external_operation_state(exposed, run=run)
    exposed["operation_state"] = state
    exposed["operation_state_source"] = source
    return exposed


def start_or_recover(
    store,
    *,
    repository,
    token,
    workflow_path=DEFAULT_WORKFLOW,
    **identity,
):
    record, should_dispatch = alpha_operation_registry.reserve_or_recover(
        store, **identity
    )
    if record.get("workflow_run_id") is not None:
        return (
            _expose_dispatch_identity(
                record, identity, repository=repository, token=token
            ),
            "RECOVERED",
        )
    if should_dispatch:
        return (
            _expose_dispatch_identity(
                record, identity, repository=repository, token=token
            ),
            "DISPATCH_REQUIRED",
        )

    runs = scan_workflow_runs(
        repository,
        token,
        workflow_path,
        not_before=record.get("created_at"),
    )
    verified_runs = alpha_refresh_contract.verify_scanned_runs(
        runs,
        record.get("correlation_ids") or [identity["dispatch_correlation_id"]],
        identity["idempotency_key"],
        identity["command_digest"],
        identity["workflow_identity"],
        identity["material_execution_inputs"],
    )
    record, should_dispatch = alpha_operation_registry.recover_after_correlation_scan(
        store, verified_runs, **identity
    )
    if record.get("workflow_run_id") is not None:
        return (
            _expose_dispatch_identity(
                record,
                identity,
                repository=repository,
                token=token,
                run=_verified_run_for_record(record, verified_runs),
            ),
            "RECOVERED",
        )
    if should_dispatch:
        return (
            _expose_dispatch_identity(
                record, identity, repository=repository, token=token
            ),
            "REDISPATCH_REQUIRED_AFTER_EMPTY_SCAN",
        )
    return (
        _expose_dispatch_identity(
            record, identity, repository=repository, token=token
        ),
        "WAITING_FOR_EXISTING_DISPATCH",
    )


def _material_inputs(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise StartOrRecoverError("material execution inputs must be a JSON object")
    return value


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Durably reserve or recover one exact Alpha refresh workflow operation."
    )
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--registry-branch", default=alpha_operation_registry.REGISTRY_BRANCH)
    parser.add_argument("--workflow-path", default=DEFAULT_WORKFLOW)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--correlation", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--command-digest", required=True)
    parser.add_argument("--workflow-identity", required=True)
    parser.add_argument("--material-inputs-json", required=True)
    parser.add_argument("--owner-token", default=None)
    args = parser.parse_args(argv)

    if not args.repository:
        raise StartOrRecoverError("repository is required")
    token = os.environ.get(args.token_env)
    if not token:
        raise StartOrRecoverError(
            f"missing token in environment variable {args.token_env}"
        )

    store = alpha_operation_registry.GitHubRegistryStore(
        args.repository, token, args.registry_branch
    )
    store.ensure_branch(args.base_sha)
    identity = {
        "dispatch_correlation_id": args.correlation,
        "idempotency_key": args.idempotency_key,
        "command_digest": args.command_digest,
        "workflow_identity": args.workflow_identity,
        "material_execution_inputs": _material_inputs(args.material_inputs_json),
        "owner_token": args.owner_token,
    }
    record, action = start_or_recover(
        store,
        repository=args.repository,
        token=token,
        workflow_path=args.workflow_path,
        **identity,
    )
    print(json.dumps({"action": action, "operation": record}))


if __name__ == "__main__":
    main()
