import re
from dataclasses import dataclass


TRUSTED_REPOSITORY = "yaelysia/looklookbigA"
TRUSTED_OWNER = "yaelysia"
AUTO_DEV_LOCK = "<!-- looklookbigA-auto-dev-lock -->"
AUTO_REVIEW_MARKER = "<!-- looklookbigA-auto-review -->"
AUTO_STATE_PREFIX = "<!-- looklookbigA-auto-state"
AUTO_STATE_SUFFIX = "-->"

AUTO_MERGE_VERDICT = "PASS_AUTOMERGE"
CHANGES_REQUIRED_VERDICT = "CHANGES_REQUIRED"
MANUAL_GATE_VERDICT = "MANUAL_GATE_REQUIRED"

HIGH_RISK_PREFIXES = (
    ".github/workflows/",
    ".github/actions/",
    ".automation-locks/",
)
HIGH_RISK_EXACT = {
    "CODEOWNERS",
    ".github/CODEOWNERS",
    ".github/dependabot.yml",
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "requirements.txt",
    "requirements-event-facts.txt",
    "Dockerfile",
    ".dockerignore",
}
HIGH_RISK_NAME_RE = re.compile(
    r"(^|/)(?:[^/]*(?:security|auth|credential|secret|permission)[^/]*)($|/)",
    re.IGNORECASE,
)
ISSUE_REF_RE = re.compile(r"(?mi)^\s*(?:refs?|closes?|fixes?)\s+#(\d+)\s*$")


def _parse_key_values(lines):
    values = {}
    for raw in lines:
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def parse_auto_state(body):
    text = str(body or "")
    start = text.find(AUTO_STATE_PREFIX)
    if start < 0:
        return {}
    start = text.find("\n", start)
    if start < 0:
        return {}
    end = text.find(AUTO_STATE_SUFFIX, start)
    if end < 0:
        return {}
    return _parse_key_values(text[start + 1 : end].splitlines())


def parse_review_verdict(body):
    text = str(body or "")
    if AUTO_REVIEW_MARKER not in text:
        return {}
    return _parse_key_values(text.splitlines())


def linked_issue_number(body, state=None):
    state = state or parse_auto_state(body)
    value = str(state.get("ISSUE") or "").strip()
    if value.isdigit():
        return int(value)
    match = ISSUE_REF_RE.search(str(body or ""))
    return int(match.group(1)) if match else None


def lock_artifacts(changed_files):
    return sorted(
        path for path in (changed_files or []) if str(path).startswith(".automation-locks/")
    )


def high_risk_paths(changed_files, base_ref="master"):
    reasons = []
    if base_ref != "master":
        reasons.append(f"NON_MASTER_BASE:{base_ref}")
    for raw_path in changed_files or []:
        path = str(raw_path)
        if path in HIGH_RISK_EXACT:
            reasons.append(path)
            continue
        if any(path.startswith(prefix) for prefix in HIGH_RISK_PREFIXES):
            reasons.append(path)
            continue
        if path.startswith("scripts/pr_lifecycle_") or path.startswith(
            "scripts/github_pr_lifecycle_"
        ):
            reasons.append(path)
            continue
        if HIGH_RISK_NAME_RE.search(path):
            reasons.append(path)
    return sorted(set(reasons))


def trusted_pr(pr):
    return (
        pr.get("state") == "open"
        and ((pr.get("user") or {}).get("login") == TRUSTED_OWNER)
        and (((pr.get("head") or {}).get("repo") or {}).get("full_name") == TRUSTED_REPOSITORY)
    )


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def promotion_decision(
    *,
    pr,
    run,
    current_base_sha,
    run_merge_parents,
    changed_files,
    issue_author=None,
):
    if not trusted_pr(pr):
        return Decision(False, "UNTRUSTED_PR")
    if not pr.get("draft"):
        return Decision(False, "PR_NOT_DRAFT")
    if run.get("name") != "Pre-merge Security Gate":
        return Decision(False, "WRONG_WORKFLOW_NAME")
    if run.get("path") != ".github/workflows/pre-merge-security-gate.yml":
        return Decision(False, "WRONG_WORKFLOW_PATH")
    if run.get("event") != "pull_request" or run.get("conclusion") != "success":
        return Decision(False, "GATE_NOT_SUCCESSFUL")
    head_sha = (pr.get("head") or {}).get("sha")
    if run.get("head_sha") != head_sha:
        return Decision(False, "STALE_GATE_HEAD")
    if list(run_merge_parents or []) != [current_base_sha, head_sha]:
        return Decision(False, "STALE_GATE_BASE_OR_MERGE_REF")
    body = pr.get("body") or ""
    if AUTO_DEV_LOCK not in body:
        return Decision(False, "AUTO_DEV_LOCK_MISSING")
    state = parse_auto_state(body)
    if state.get("CHECKPOINT") != "READY_FOR_REVIEW_PREPARED":
        return Decision(False, "NOT_PREPARED_FOR_REVIEW")
    if state.get("LAST_HEAD") != head_sha:
        return Decision(False, "AUTO_STATE_HEAD_STALE")
    if lock_artifacts(changed_files):
        return Decision(False, "LOCK_ARTIFACT_NOT_REMOVED")
    issue_number = linked_issue_number(body, state)
    if issue_number is not None and issue_author != TRUSTED_OWNER:
        return Decision(False, "UNTRUSTED_LINKED_ISSUE")
    return Decision(True, "PROMOTE")


def verdict_binding_decision(*, pr, comment, current_base_sha, issue_author=None):
    if not trusted_pr(pr):
        return Decision(False, "UNTRUSTED_PR")
    if (comment.get("user") or {}).get("login") != TRUSTED_OWNER:
        return Decision(False, "UNTRUSTED_VERDICT_AUTHOR")
    parsed = parse_review_verdict(comment.get("body"))
    if not parsed:
        return Decision(False, "VERDICT_MARKER_MISSING")
    head_sha = (pr.get("head") or {}).get("sha")
    if parsed.get("HEAD_SHA") != head_sha:
        return Decision(False, "STALE_VERDICT_HEAD")
    if parsed.get("BASE_SHA") != current_base_sha:
        return Decision(False, "STALE_VERDICT_BASE")
    issue_number = linked_issue_number(pr.get("body"))
    if issue_number is not None and issue_author != TRUSTED_OWNER:
        return Decision(False, "UNTRUSTED_LINKED_ISSUE")
    verdict = parsed.get("VERDICT") or ""
    if not verdict:
        return Decision(False, "VERDICT_MISSING")
    return Decision(True, verdict)


def auto_merge_decision(
    *,
    pr,
    comment,
    current_base_sha,
    changed_files,
    exact_gate_valid,
    issue_author=None,
):
    bound = verdict_binding_decision(
        pr=pr,
        comment=comment,
        current_base_sha=current_base_sha,
        issue_author=issue_author,
    )
    if not bound.allowed:
        return bound
    if bound.reason != AUTO_MERGE_VERDICT:
        return Decision(False, f"VERDICT_{bound.reason}")
    if pr.get("draft"):
        return Decision(False, "PR_IS_DRAFT")
    if pr.get("mergeable") is not True:
        return Decision(False, "PR_NOT_MERGEABLE")
    if lock_artifacts(changed_files):
        return Decision(False, "LOCK_ARTIFACT_NOT_REMOVED")
    risky = high_risk_paths(changed_files, (pr.get("base") or {}).get("ref") or "")
    if risky:
        return Decision(False, "HIGH_RISK_MANUAL_GATE")
    if not exact_gate_valid:
        return Decision(False, "EXACT_GATE_NOT_VALID")
    return Decision(True, "MERGE")


def should_close_issue_on_merge(pr):
    state = parse_auto_state(pr.get("body"))
    return str(state.get("ISSUE_CLOSE_ON_MERGE") or "").strip().lower() == "true"
