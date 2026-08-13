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
    ref = f"refs/heads/{base_ref}"
    ref_name = ((ruleset.get("conditions") or {}).get("ref_name") or {})
    includes = [str(value) for value in ref_name.get("include") or []]
    excludes = [str(value) for value in ref_name.get("exclude") or []]
    return ref in includes and ref not in excludes

def _strict_required_check_rule(ruleset):
    for rule in ruleset.get("rules") or []:
        if rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters") or {}
        if params.get("strict_required_status_checks_policy") is not True:
            continue
        contexts = {str(item.get("context") or "") for item in params.get("required_status_checks") or [] if isinstance(item, dict)}
        if REQUIRED_STATUS_CONTEXT in contexts:
            return True
    return False

def atomic_latest_base_prerequisite(rulesets, base_ref):
    for ruleset in rulesets or []:
        if not _ruleset_selects_exact_ref(ruleset, base_ref):
            continue
        if ruleset.get("bypass_actors"):
            continue
        if ruleset.get("current_user_can_bypass") not in (None, "never"):
            continue
        if _strict_required_check_rule(ruleset):
            return True
    return False

def _active_ruleset_details():
    summaries = _request("GET", _repo_path("/rulesets?includes_parents=true&per_page=100")) or []
    details = []
    for summary in summaries:
        if summary.get("target") != "branch" or summary.get("enforcement") != "active":
            continue
        ruleset_id = summary.get("id")
        if ruleset_id is None:
            continue
        details.append(_request("GET", _repo_path(f"/rulesets/{int(ruleset_id)}")) or {})
    return details

def verify_pass_automerge_event(event):
    if REPOSITORY != TRUSTED_REPOSITORY:
        raise PrerequisiteError(f"unexpected repository: {REPOSITORY}")
    comment = event.get("comment") or {}
    if parse_verdict(comment.get("body")) != AUTO_MERGE_VERDICT:
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
    if not atomic_latest_base_prerequisite(_active_ruleset_details(), base_ref):
        raise PrerequisiteError("ATOMIC_LATEST_BASE_PREREQUISITE_MISSING")
    return f"STRICT_LATEST_BASE_ENFORCED:{base_ref}"

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
