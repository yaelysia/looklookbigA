import json
import os
import urllib.parse
import urllib.request
from datetime import timedelta

import alpha_operation_registry
import alpha_refresh_contract


DEFAULT_WORKFLOW = ".github/workflows/realtime-quotes.yml"
MAX_SCAN_PAGES = 100


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


def _expose_dispatch_identity(record, identity):
    exposed = dict(record)
    exposed["operation_identity_fingerprint"] = _operation_fingerprint(identity)
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
    if record.get("workflow_run_id"):
        return _expose_dispatch_identity(record, identity), "RECOVERED"
    if should_dispatch:
        return _expose_dispatch_identity(record, identity), "DISPATCH_REQUIRED"

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
    if record.get("workflow_run_id"):
        return _expose_dispatch_identity(record, identity), "RECOVERED"
    if should_dispatch:
        return (
            _expose_dispatch_identity(record, identity),
            "REDISPATCH_REQUIRED_AFTER_EMPTY_SCAN",
        )
    return _expose_dispatch_identity(record, identity), "WAITING_FOR_EXISTING_DISPATCH"


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
