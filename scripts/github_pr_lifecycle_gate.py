import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pr_lifecycle_policy as policy


API_ROOT = "https://api.github.com"
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", policy.TRUSTED_REPOSITORY)
TOKEN = os.environ.get("GITHUB_TOKEN", "")
EVENT_PATH = os.environ.get("GITHUB_EVENT_PATH", "")


class GateError(RuntimeError):
    pass


def _request(method, path, payload=None):
    if not TOKEN:
        raise GateError("GITHUB_TOKEN is required")
    url = path if path.startswith("https://") else f"{API_ROOT}{path}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "looklookbigA-pr-lifecycle-gate",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise GateError(f"GitHub API {method} {path} failed HTTP {exc.code}: {body[:500]}") from exc
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _graphql(query, variables):
    result = _request("POST", "/graphql", {"query": query, "variables": variables})
    errors = (result or {}).get("errors") or []
    if errors:
        raise GateError(f"GraphQL failed: {errors}")
    return (result or {}).get("data") or {}


def _load_event():
    if not EVENT_PATH:
        raise GateError("GITHUB_EVENT_PATH is required")
    with open(EVENT_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _repo_path(suffix):
    return f"/repos/{REPOSITORY}{suffix}"


def _get_pr(number):
    return _request("GET", _repo_path(f"/pulls/{int(number)}"))


def _current_base_sha(pr):
    base_ref = (pr.get("base") or {}).get("ref")
    if not base_ref:
        raise GateError("PR base ref missing")
    encoded = urllib.parse.quote(base_ref, safe="")
    ref = _request("GET", _repo_path(f"/git/ref/heads/{encoded}"))
    return ((ref or {}).get("object") or {}).get("sha")


def _changed_files(number):
    files = []
    page = 1
    while True:
        rows = _request(
            "GET",
            _repo_path(f"/pulls/{int(number)}/files?per_page=100&page={page}"),
        ) or []
        files.extend(str(item.get("filename") or "") for item in rows if item.get("filename"))
        if len(rows) < 100:
            break
        page += 1
    return files


def _issue_author(pr):
    number = policy.linked_issue_number(pr.get("body"))
    if number is None:
        return None
    issue = _request("GET", _repo_path(f"/issues/{number}")) or {}
    return ((issue.get("user") or {}).get("login"))


def _get_run(run_id):
    return _request("GET", _repo_path(f"/actions/runs/{int(run_id)}")) or {}


def _merge_ref_parents(run, pr_number):
    expected_ref = f"refs/pull/{int(pr_number)}/merge"
    for item in run.get("referenced_workflows") or []:
        if item.get("ref") != expected_ref:
            continue
        merge_sha = item.get("sha")
        if not merge_sha:
            continue
        commit = _request("GET", _repo_path(f"/git/commits/{merge_sha}")) or {}
        return [parent.get("sha") for parent in commit.get("parents") or []]
    return []


def _run_is_exact(run, pr, current_base_sha):
    if run.get("name") != "Pre-merge Security Gate":
        return False
    if run.get("path") != ".github/workflows/pre-merge-security-gate.yml":
        return False
    if run.get("event") != "pull_request" or run.get("conclusion") != "success":
        return False
    if run.get("head_sha") != ((pr.get("head") or {}).get("sha")):
        return False
    parents = _merge_ref_parents(run, pr.get("number"))
    return parents == [current_base_sha, (pr.get("head") or {}).get("sha")]


def _find_exact_gate_run(pr, current_base_sha):
    head_sha = (pr.get("head") or {}).get("sha")
    params = urllib.parse.urlencode(
        {
            "head_sha": head_sha,
            "event": "pull_request",
            "status": "success",
            "per_page": 100,
        }
    )
    result = _request("GET", _repo_path(f"/actions/runs?{params}")) or {}
    runs = result.get("workflow_runs") or []
    runs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    for summary in runs:
        if summary.get("name") != "Pre-merge Security Gate":
            continue
        run = _get_run(summary.get("id"))
        if _run_is_exact(run, pr, current_base_sha):
            return run
    return None


def _find_pr_for_workflow_run(run):
    branch = str(run.get("head_branch") or "")
    head_sha = str(run.get("head_sha") or "")
    if not branch or not head_sha:
        return None
    params = urllib.parse.urlencode(
        {
            "state": "open",
            "head": f"{policy.TRUSTED_OWNER}:{branch}",
            "per_page": 20,
        }
    )
    rows = _request("GET", _repo_path(f"/pulls?{params}")) or []
    exact = [row for row in rows if ((row.get("head") or {}).get("sha") == head_sha)]
    if len(exact) != 1:
        return None
    return _get_pr(exact[0].get("number"))


def _mark_ready(pr):
    node_id = pr.get("node_id")
    if not node_id:
        raise GateError("PR node_id missing")
    query = """
    mutation($id: ID!) {
      markPullRequestReadyForReview(input: {pullRequestId: $id}) {
        pullRequest { number isDraft }
      }
    }
    """
    data = _graphql(query, {"id": node_id})
    value = ((data.get("markPullRequestReadyForReview") or {}).get("pullRequest") or {})
    if value.get("isDraft") is not False:
        raise GateError(f"Ready transition did not complete: {value}")


def _convert_to_draft(pr):
    node_id = pr.get("node_id")
    if not node_id:
        raise GateError("PR node_id missing")
    query = """
    mutation($id: ID!) {
      convertPullRequestToDraft(input: {pullRequestId: $id}) {
        pullRequest { number isDraft }
      }
    }
    """
    data = _graphql(query, {"id": node_id})
    value = ((data.get("convertPullRequestToDraft") or {}).get("pullRequest") or {})
    if value.get("isDraft") is not True:
        raise GateError(f"Draft transition did not complete: {value}")


def _refresh_mergeability(number):
    pr = _get_pr(number)
    for _ in range(3):
        if pr.get("mergeable") is not None:
            return pr
        time.sleep(1)
        pr = _get_pr(number)
    return pr


def _close_linked_issue_if_requested(pr):
    if not policy.should_close_issue_on_merge(pr):
        return
    number = policy.linked_issue_number(pr.get("body"))
    if number is None:
        return
    issue = _request("GET", _repo_path(f"/issues/{number}")) or {}
    if ((issue.get("user") or {}).get("login")) != policy.TRUSTED_OWNER:
        raise GateError("Refusing to close untrusted linked issue")
    if issue.get("state") == "open":
        _request(
            "PATCH",
            _repo_path(f"/issues/{number}"),
            {"state": "closed", "state_reason": "completed"},
        )
        print(f"PR_LIFECYCLE issue_closed={number}", flush=True)


def _dispatch_postcheck(merged_sha, pr_number):
    _request(
        "POST",
        _repo_path("/dispatches"),
        {
            "event_type": "looklookbiga-native-merge-postcheck",
            "client_payload": {
                "merged_sha": merged_sha,
                "pr_number": int(pr_number),
            },
        },
    )


def promote():
    event = _load_event()
    run = None
    if isinstance(event.get("workflow_run"), dict):
        run = _get_run(event["workflow_run"].get("id"))
        pr = _find_pr_for_workflow_run(run)
    elif isinstance(event.get("pull_request"), dict):
        pr = _get_pr(event.get("number") or event["pull_request"].get("number"))
    else:
        print("PR_PROMOTION no_candidate_event", flush=True)
        return 0

    if not pr:
        print("PR_PROMOTION no_exact_pr_candidate", flush=True)
        return 0
    if not pr.get("draft"):
        print(f"PR_PROMOTION pr={pr.get('number')} already_ready", flush=True)
        return 0

    current_base_sha = _current_base_sha(pr)
    if run is None or not _run_is_exact(run, pr, current_base_sha):
        run = _find_exact_gate_run(pr, current_base_sha)
    if run is None:
        print(f"PR_PROMOTION pr={pr.get('number')} decision=NO_EXACT_GATE", flush=True)
        return 0

    changed_files = _changed_files(pr.get("number"))
    decision = policy.promotion_decision(
        pr=pr,
        run=run,
        current_base_sha=current_base_sha,
        run_merge_parents=_merge_ref_parents(run, pr.get("number")),
        changed_files=changed_files,
        issue_author=_issue_author(pr),
    )
    print(
        f"PR_PROMOTION pr={pr.get('number')} head={(pr.get('head') or {}).get('sha')} "
        f"base={current_base_sha} run={run.get('id')} decision={decision.reason}",
        flush=True,
    )
    if not decision.allowed:
        return 0
    _mark_ready(pr)
    print(f"PR_PROMOTION promoted pr={pr.get('number')}", flush=True)
    return 0


def review_gate():
    event = _load_event()
    issue = event.get("issue") or {}
    comment_event = event.get("comment") or {}
    if not issue.get("pull_request") or not comment_event.get("id"):
        print("PR_REVIEW_GATE non_pr_comment", flush=True)
        return 0

    comment = _request("GET", _repo_path(f"/issues/comments/{comment_event['id']}")) or {}
    parsed = policy.parse_review_verdict(comment.get("body"))
    if not parsed:
        print("PR_REVIEW_GATE no_structured_verdict", flush=True)
        return 0

    pr = _get_pr(issue.get("number"))
    if not pr or pr.get("state") != "open":
        print("PR_REVIEW_GATE pr_not_open", flush=True)
        return 0
    current_base_sha = _current_base_sha(pr)
    issue_author = _issue_author(pr)
    bound = policy.verdict_binding_decision(
        pr=pr,
        comment=comment,
        current_base_sha=current_base_sha,
        issue_author=issue_author,
    )
    print(
        f"PR_REVIEW_GATE pr={pr.get('number')} head={(pr.get('head') or {}).get('sha')} "
        f"base={current_base_sha} decision={bound.reason}",
        flush=True,
    )
    if not bound.allowed:
        return 0

    verdict = bound.reason
    if verdict == policy.CHANGES_REQUIRED_VERDICT:
        if not pr.get("draft"):
            _convert_to_draft(pr)
            print(f"PR_REVIEW_GATE converted_to_draft pr={pr.get('number')}", flush=True)
        return 0
    if verdict != policy.AUTO_MERGE_VERDICT:
        return 0

    pr = _refresh_mergeability(pr.get("number"))
    current_base_sha = _current_base_sha(pr)
    changed_files = _changed_files(pr.get("number"))
    exact_run = _find_exact_gate_run(pr, current_base_sha)
    decision = policy.auto_merge_decision(
        pr=pr,
        comment=comment,
        current_base_sha=current_base_sha,
        changed_files=changed_files,
        exact_gate_valid=exact_run is not None,
        issue_author=_issue_author(pr),
    )
    print(
        f"PR_REVIEW_GATE pr={pr.get('number')} merge_decision={decision.reason} "
        f"exact_gate_run={(exact_run or {}).get('id')}",
        flush=True,
    )
    if not decision.allowed:
        return 0

    head_sha = (pr.get("head") or {}).get("sha")
    result = _request(
        "PUT",
        _repo_path(f"/pulls/{pr.get('number')}/merge"),
        {"sha": head_sha, "merge_method": "merge"},
    ) or {}
    if result.get("merged") is not True or not result.get("sha"):
        raise GateError(f"Merge rejected: {result}")
    merged_sha = result["sha"]
    print(f"PR_REVIEW_GATE merged pr={pr.get('number')} sha={merged_sha}", flush=True)
    _close_linked_issue_if_requested(pr)
    _dispatch_postcheck(merged_sha, pr.get("number"))
    print(f"PR_REVIEW_GATE postcheck_dispatched sha={merged_sha}", flush=True)
    return 0


def main(argv=None):
    argv = list(argv or sys.argv[1:])
    if len(argv) != 1 or argv[0] not in {"promote", "review"}:
        raise SystemExit("usage: github_pr_lifecycle_gate.py promote|review")
    try:
        return promote() if argv[0] == "promote" else review_gate()
    except GateError as exc:
        print(f"PR_LIFECYCLE_GATE_ERROR {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
