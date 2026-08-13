import json
import os
import sys
import urllib.error
import urllib.request

API_ROOT = "https://api.github.com"
TRUSTED_REPOSITORY = "yaelysia/looklookbigA"
AUTO_REVIEW_MARKER = "<!-- looklookbigA-auto-review -->"
AUTO_MERGE_VERDICT = "PASS_AUTOMERGE"
REQUIRED_STATUS_CONTEXT = "pre-merge-security-gate"
STRICT_MERGE_API = "STRICT_MERGE_API"
VERIFIED_MERGE_REF_FF = "VERIFIED_MERGE_REF_FAST_FORWARD"
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", TRUSTED_REPOSITORY)
TOKEN = os.environ.get("GITHUB_TOKEN", "")
EVENT_PATH = os.environ.get("GITHUB_EVENT_PATH", "")


class PrerequisiteError(RuntimeError):
    pass


def _request(method, path):
    if not TOKEN:
        raise PrerequisiteError("GITHUB_TOKEN is required")
    url = path if path.startswith("https://") else f"{API_ROOT}{path}"
    request = urllib.request.Request(url, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "looklookbigA-pr-atomic-base-prerequisite",
    })
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise PrerequisiteError(f"GitHub API {method} {path} failed HTTP {exc.code}: {body[:500]}") from exc
    return json.loads(raw.decode("utf-8")) if raw else None


def _load_event():
    if not EVENT_PATH:
        raise PrerequisiteError("GITHUB_EVENT_PATH is required")
    with open(EVENT_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _repo_path(suffix):
    return f"/repos/{REPOSITORY}{suffix}"


def parse_verdict(body):
    text = str(body or "")
    if AUTO_REVIEW_MARKER not in text:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("VERDICT="):
            return line.split("=", 1)[1].strip()
    return ""


def _ruleset_selects_exact_ref(ruleset, base_ref):
    if ruleset.get("target") != "branch" or ruleset.get("enforcement") != "active":
        return False
    names = ((ruleset.get("conditions") or {}).get("ref_name") or {})
    ref = f"refs/heads/{base_ref}"
    return ref in [str(v) for v in names.get("include") or []] and ref not in [str(v) for v in names.get("exclude") or []]


def _no_bypass(ruleset):
    return not ruleset.get("bypass_actors") and ruleset.get("current_user_can_bypass") in (None, "never")


def _required_check_params(ruleset):
    for rule in ruleset.get("rules") or []:
        if rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters") or {}
        contexts = {str(v.get("context") or "") for v in params.get("required_status_checks") or [] if isinstance(v, dict)}
        if REQUIRED_STATUS_CONTEXT in contexts:
            return params
    return None


def _has_rule(ruleset, rule_type):
    return any(rule.get("type") == rule_type for rule in ruleset.get("rules") or [])


def atomic_merge_transport(rulesets, base_ref):
    for ruleset in rulesets or []:
        if not _ruleset_selects_exact_ref(ruleset, base_ref) or not _no_bypass(ruleset):
            continue
        params = _required_check_params(ruleset)
        if not params:
            continue
        if params.get("strict_required_status_checks_policy") is True:
            return STRICT_MERGE_API
        if _has_rule(ruleset, "non_fast_forward") and _has_rule(ruleset, "pull_request"):
            return VERIFIED_MERGE_REF_FF
    return ""


def atomic_latest_base_prerequisite(rulesets, base_ref):
    return bool(atomic_merge_transport(rulesets, base_ref))


def _active_ruleset_details():
    summaries = _request("GET", _repo_path("/rulesets?includes_parents=true&per_page=100")) or []
    out = []
    for summary in summaries:
        if summary.get("target") != "branch" or summary.get("enforcement") != "active" or summary.get("id") is None:
            continue
        out.append(_request("GET", _repo_path(f"/rulesets/{int(summary['id'])}")) or {})
    return out


def verify_pass_automerge_event(event):
    if REPOSITORY != TRUSTED_REPOSITORY:
        raise PrerequisiteError(f"unexpected repository: {REPOSITORY}")
    if parse_verdict((event.get("comment") or {}).get("body")) != AUTO_MERGE_VERDICT:
        return "NOT_PASS_AUTOMERGE"
    issue = event.get("issue") or {}
    if not issue.get("pull_request") or not issue.get("number"):
        raise PrerequisiteError("PASS_AUTOMERGE event is not a pull request comment")
    pr = _request("GET", _repo_path(f"/pulls/{int(issue['number'])}")) or {}
    if pr.get("state") != "open":
        raise PrerequisiteError("PASS_AUTOMERGE pull request is not open")
    base_ref = str((pr.get("base") or {}).get("ref") or "")
    if not base_ref:
        raise PrerequisiteError("pull request base ref is missing")
    transport = atomic_merge_transport(_active_ruleset_details(), base_ref)
    if not transport:
        raise PrerequisiteError("ATOMIC_MERGE_TRANSPORT_MISSING")
    return f"ATOMIC_MERGE_TRANSPORT:{transport}:{base_ref}"


def main():
    try:
        result = verify_pass_automerge_event(_load_event())
    except PrerequisiteError as exc:
        print(f"PR_ATOMIC_BASE_PREREQUISITE_ERROR {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"PR_ATOMIC_BASE_PREREQUISITE {result}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
