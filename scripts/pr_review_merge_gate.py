import sys
import time
import urllib.parse

import github_pr_lifecycle_gate as legacy
import pr_lifecycle_atomic_base as atomic
import pr_lifecycle_policy as policy


def _run_jobs(run_id):
    jobs = []
    page = 1
    while True:
        result = legacy._request("GET", legacy._repo_path(f"/actions/runs/{int(run_id)}/jobs?per_page=100&page={page}")) or {}
        rows = result.get("jobs") or []
        jobs.extend(rows)
        if len(rows) < 100:
            break
        page += 1
    return jobs


def _high_risk_checks_valid(run, changed_files, base_ref):
    requirements = policy.high_risk_evidence_requirements(changed_files, base_ref)
    if not requirements["jobs"] and not requirements["steps"]:
        return True
    jobs = _run_jobs(run.get("id"))
    job_state = {str(job.get("name") or ""): str(job.get("conclusion") or "") for job in jobs}
    step_state = {}
    for job in jobs:
        for step in job.get("steps") or []:
            step_state[str(step.get("name") or "")] = str(step.get("conclusion") or "")
    def job_ok(required):
        return any(conclusion == "success" and (name == required or name.startswith(required + " /")) for name, conclusion in job_state.items())
    return all(job_ok(name) for name in requirements["jobs"]) and all(step_state.get(name) == "success" for name in requirements["steps"])


def _validated_merge_ref(run, pr_number, base_sha, head_sha):
    expected_ref = f"refs/pull/{int(pr_number)}/merge"
    matches = [item for item in run.get("referenced_workflows") or [] if item.get("ref") == expected_ref and item.get("sha")]
    if len(matches) != 1:
        raise legacy.GateError(f"expected exactly one validated merge ref, got {len(matches)}")
    merge_sha = str(matches[0]["sha"])
    commit = legacy._request("GET", legacy._repo_path(f"/git/commits/{merge_sha}")) or {}
    parents = [parent.get("sha") for parent in commit.get("parents") or []]
    if parents != [base_sha, head_sha]:
        raise legacy.GateError(f"VALIDATED_MERGE_REF_STALE expected={[base_sha, head_sha]} observed={parents}")
    if not ((commit.get("tree") or {}).get("sha")):
        raise legacy.GateError("validated merge ref tree missing")
    return {"sha": merge_sha, "parents": parents, "tree_sha": commit["tree"]["sha"]}


def _preflight(pr_number, head_sha, base_sha):
    pr = legacy._get_pr(pr_number)
    current_base = legacy._current_base_sha(pr)
    if pr.get("state") != "open" or pr.get("draft"):
        raise legacy.GateError("PREMERGE_STATE_CHANGED")
    if (pr.get("head") or {}).get("sha") != head_sha:
        raise legacy.GateError("PREMERGE_HEAD_MOVED")
    if current_base != base_sha:
        raise legacy.GateError("PREMERGE_BASE_MOVED")
    if pr.get("mergeable") is not True:
        raise legacy.GateError("PREMERGE_NOT_MERGEABLE")
    return pr


def _strict_merge(pr, head_sha, base_sha):
    _preflight(pr.get("number"), head_sha, base_sha)
    result = legacy._request("PUT", legacy._repo_path(f"/pulls/{pr.get('number')}/merge"), {"sha": head_sha, "merge_method": "merge"}) or {}
    if result.get("merged") is not True or not result.get("sha"):
        raise legacy.GateError(f"Merge rejected: {result}")
    return str(result["sha"])


def _merge_ref_fast_forward(pr, exact_run, head_sha, base_sha):
    identity = _validated_merge_ref(exact_run, pr.get("number"), base_sha, head_sha)
    preflight = _preflight(pr.get("number"), head_sha, base_sha)
    if legacy._find_exact_gate_run(preflight, base_sha) is None:
        raise legacy.GateError("EXACT_GATE_LOST_BEFORE_REF_UPDATE")
    base_ref = str((preflight.get("base") or {}).get("ref") or "")
    encoded = urllib.parse.quote(base_ref, safe="")
    result = legacy._request("PATCH", legacy._repo_path(f"/git/refs/heads/{encoded}"), {"sha": identity["sha"], "force": False}) or {}
    observed = ((result.get("object") or {}).get("sha"))
    if observed != identity["sha"]:
        raise legacy.GateError(f"REF_UPDATE_IDENTITY_MISMATCH expected={identity['sha']} observed={observed}")
    for _ in range(5):
        final_pr = legacy._get_pr(pr.get("number")) or {}
        if final_pr.get("merged") is True or (final_pr.get("state") == "closed" and final_pr.get("merge_commit_sha") == identity["sha"]):
            return identity["sha"]
        time.sleep(1)
    raise legacy.GateError("REF_UPDATE_LANDED_BUT_PR_MERGED_STATE_NOT_OBSERVED")


def _verify_and_postcheck(pr, merged_sha, base_sha, head_sha):
    commit = legacy._request("GET", legacy._repo_path(f"/git/commits/{merged_sha}")) or {}
    parents = [parent.get("sha") for parent in commit.get("parents") or []]
    decision = policy.merged_parent_decision(base_sha, head_sha, parents)
    legacy._dispatch_postcheck(merged_sha, pr.get("number"), base_sha, head_sha)
    print(f"PR_REVIEW_GATE postcheck_dispatched sha={merged_sha}", flush=True)
    if not decision.allowed:
        raise legacy.GateError(f"{decision.reason}: expected={[base_sha, head_sha]} observed={parents}")
    legacy._close_linked_issue_if_requested(pr)


def review_gate():
    event = legacy._load_event()
    issue = event.get("issue") or {}
    comment_event = event.get("comment") or {}
    if not issue.get("pull_request") or not comment_event.get("id"):
        print("PR_REVIEW_GATE non_pr_comment", flush=True)
        return 0
    comment = legacy._request("GET", legacy._repo_path(f"/issues/comments/{comment_event['id']}")) or {}
    if not policy.parse_review_verdict(comment.get("body")):
        print("PR_REVIEW_GATE no_structured_verdict", flush=True)
        return 0
    pr = legacy._get_pr(issue.get("number"))
    if not pr or pr.get("state") != "open":
        print("PR_REVIEW_GATE pr_not_open", flush=True)
        return 0
    current_base = legacy._current_base_sha(pr)
    bound = policy.verdict_binding_decision(pr=pr, comment=comment, current_base_sha=current_base, issue_author=legacy._issue_author(pr))
    print(f"PR_REVIEW_GATE pr={pr.get('number')} head={(pr.get('head') or {}).get('sha')} base={current_base} decision={bound.reason}", flush=True)
    if not bound.allowed:
        return 0
    if bound.reason == policy.CHANGES_REQUIRED_VERDICT:
        if not pr.get("draft"):
            legacy._convert_to_draft(pr)
            print(f"PR_REVIEW_GATE converted_to_draft pr={pr.get('number')}", flush=True)
        return 0
    if bound.reason != policy.AUTO_MERGE_VERDICT:
        return 0

    pr = legacy._refresh_mergeability(pr.get("number"))
    current_base = legacy._current_base_sha(pr)
    changed_files = legacy._changed_files(pr.get("number"))
    exact_run = legacy._find_exact_gate_run(pr, current_base)
    base_ref = str((pr.get("base") or {}).get("ref") or "")
    risk_ok = bool(exact_run) and _high_risk_checks_valid(exact_run, changed_files, base_ref)
    decision = policy.auto_merge_decision(pr=pr, comment=comment, current_base_sha=current_base, changed_files=changed_files, exact_gate_valid=exact_run is not None, high_risk_checks_valid=risk_ok, issue_author=legacy._issue_author(pr))
    print(f"PR_REVIEW_GATE pr={pr.get('number')} merge_decision={decision.reason} exact_gate_run={(exact_run or {}).get('id')} high_risk_checks={risk_ok}", flush=True)
    if not decision.allowed:
        return 0

    head_sha = str((pr.get("head") or {}).get("sha") or "")
    transport = atomic.atomic_merge_transport(atomic._active_ruleset_details(), base_ref)
    if not transport:
        raise legacy.GateError("ATOMIC_MERGE_TRANSPORT_MISSING")
    print(f"PR_REVIEW_GATE transport={transport} base={base_ref}", flush=True)
    if transport == atomic.STRICT_MERGE_API:
        merged_sha = _strict_merge(pr, head_sha, current_base)
    elif transport == atomic.VERIFIED_MERGE_REF_FF:
        merged_sha = _merge_ref_fast_forward(pr, exact_run, head_sha, current_base)
    else:
        raise legacy.GateError(f"unknown atomic merge transport: {transport}")
    print(f"PR_REVIEW_GATE merged pr={pr.get('number')} sha={merged_sha}", flush=True)
    _verify_and_postcheck(pr, merged_sha, current_base, head_sha)
    return 0


def main():
    try:
        return review_gate()
    except (legacy.GateError, atomic.PrerequisiteError) as exc:
        print(f"PR_REVIEW_MERGE_GATE_ERROR {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
