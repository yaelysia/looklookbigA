import json
import os
import urllib.parse
import urllib.request


API_ROOT = "https://api.github.com"
WORKFLOW_FILE = "realtime-quotes.yml"
WORKFLOW_PATH = ".github/workflows/realtime-quotes.yml"


def _request_json(path, token, timeout=5):
    url = API_ROOT + path
    if not url.startswith("https://api.github.com/"):
        raise ValueError("GitHub Actions history resolver only permits api.github.com HTTPS")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "looklookbigA-history-continuity",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def select_previous_success(payload, current_run_id=None):
    try:
        current = int(current_run_id) if current_run_id not in (None, "") else None
    except (TypeError, ValueError):
        current = None
    candidates = []
    for run in (payload or {}).get("workflow_runs") or []:
        if not isinstance(run, dict):
            continue
        try:
            run_id = int(run.get("id"))
        except (TypeError, ValueError):
            continue
        if current is not None and run_id == current:
            continue
        if run.get("conclusion") != "success":
            continue
        if run.get("head_branch") != "master":
            continue
        path = str(run.get("path") or "")
        if path and path != WORKFLOW_PATH:
            continue
        candidates.append(run)
    if not candidates:
        return None
    candidates.sort(
        key=lambda run: (
            str(run.get("run_started_at") or run.get("created_at") or ""),
            int(run.get("id") or 0),
        ),
        reverse=True,
    )
    return candidates[0]


def _eligible_success(run):
    if not isinstance(run, dict) or run.get("conclusion") != "success":
        return False
    if run.get("head_branch") != "master":
        return False
    path = str(run.get("path") or "")
    return not path or path == WORKFLOW_PATH


def resolve_previous_success(
    repository,
    token,
    current_run_id=None,
    current_run_attempt=None,
):
    repository = str(repository or "").strip()
    if "/" not in repository or repository.count("/") != 1:
        raise ValueError("GITHUB_REPOSITORY must be owner/repo")
    owner, repo = repository.split("/", 1)
    try:
        current = int(current_run_id) if current_run_id not in (None, "") else None
        attempt = int(current_run_attempt) if current_run_attempt not in (None, "") else 1
    except (TypeError, ValueError) as exc:
        raise ValueError("current workflow run id/attempt must be integers") from exc
    if attempt < 1:
        raise ValueError("current workflow run attempt must be positive")

    repository_path = (
        "/repos/"
        + urllib.parse.quote(owner, safe="")
        + "/"
        + urllib.parse.quote(repo, safe="")
    )
    if current is not None:
        for previous_attempt in range(attempt - 1, 0, -1):
            candidate = _request_json(
                f"{repository_path}/actions/runs/{current}/attempts/{previous_attempt}",
                token,
            )
            if _eligible_success(candidate):
                selected = dict(candidate)
                selected["run_attempt"] = previous_attempt
                selected["baseline_kind"] = "SAME_RUN_PREVIOUS_SUCCESSFUL_ATTEMPT"
                return selected

    endpoint = (
        repository_path
        + "/actions/workflows/"
        + urllib.parse.quote(WORKFLOW_FILE, safe="")
        + "/runs?branch=master&status=success&per_page=20"
    )
    payload = _request_json(endpoint, token)
    selected = select_previous_success(payload, current_run_id=current)
    if selected:
        selected = dict(selected)
        selected["run_attempt"] = int(selected.get("run_attempt") or 1)
        selected["baseline_kind"] = "PREVIOUS_SUCCESSFUL_RUN"
    return selected


def _write_outputs(path, values):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            text = "" if value is None else str(value)
            if "\n" in text or "\r" in text:
                raise ValueError(f"invalid multiline GitHub output for {key}")
            handle.write(f"{key}={text}\n")


def main():
    repository = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    current_run_id = os.environ.get("GITHUB_RUN_ID")
    current_run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
    previous = resolve_previous_success(
        repository,
        token,
        current_run_id=current_run_id,
        current_run_attempt=current_run_attempt,
    )
    if not previous:
        outputs = {
            "run_id": "",
            "run_attempt": "",
            "started_at": "",
            "head_sha": "",
            "baseline_kind": "",
        }
        print("HISTORY_BASELINE_RESOLVE previous_success=NONE", flush=True)
    else:
        outputs = {
            "run_id": previous.get("id"),
            "run_attempt": previous.get("run_attempt") or 1,
            "started_at": previous.get("run_started_at") or previous.get("created_at") or "",
            "head_sha": previous.get("head_sha") or "",
            "baseline_kind": previous.get("baseline_kind") or "",
        }
        print(
            "HISTORY_BASELINE_RESOLVE "
            f"run_id={outputs['run_id']} attempt={outputs['run_attempt']} "
            f"kind={outputs['baseline_kind']} started_at={outputs['started_at']} "
            f"head_sha={outputs['head_sha']}",
            flush=True,
        )
    _write_outputs(os.environ.get("GITHUB_OUTPUT"), outputs)


if __name__ == "__main__":
    main()
