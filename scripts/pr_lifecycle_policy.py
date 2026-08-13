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
ISSUE_REF_RE = re.compile(r"(?mi)^\s*(?:refs?|closes?|fixes?)\s+#(\d+)\s*$")
DEPENDENCY_RE = re.compile(r"^(?:pyproject\.toml|uv\.lock|poetry\.lock|Pipfile(?:\.lock)?|package(?:-lock)?\.json|pnpm-lock\.yaml|yarn\.lock|requirements(?:-[A-Za-z0-9_.-]+)?\.txt)$", re.I)
DOCKER_RE = re.compile(r"^(?:Dockerfile(?:\.[A-Za-z0-9_.-]+)?|docker-compose(?:\.[A-Za-z0-9_.-]+)?\.ya?ml|compose(?:\.[A-Za-z0-9_.-]+)?\.ya?ml|\.dockerignore)$", re.I)
RISK_NAME_RE = re.compile(r"(?:security|auth|credential|secret|permission|ruleset|branch[_-]?protection|release|stable[_-]?v?1|deploy|dependabot)", re.I)
RISK_EXACT = {"CODEOWNERS", ".github/CODEOWNERS", ".github/dependabot.yml"}
RISK_JOBS = {"Safety tests", "Reusable smoke", "pre-merge-security-gate"}
RISK_STEPS = {"Run workflow supply-chain and transport tests"}
LIFECYCLE_STEPS = {"Run PR lifecycle policy tests", "Run PR atomic latest-base prerequisite tests"}


def _parse(lines):
    out = {}
    for raw in lines:
        line = raw.strip()
        if line and "=" in line:
            key, value = line.split("=", 1)
            if key.strip():
                out[key.strip()] = value.strip()
    return out


def parse_auto_state(body):
    text = str(body or "")
    start = text.find(AUTO_STATE_PREFIX)
    if start < 0:
        return {}
    start = text.find("\n", start)
    end = text.find(AUTO_STATE_SUFFIX, start)
    return _parse(text[start + 1:end].splitlines()) if start >= 0 and end >= 0 else {}


def parse_review_verdict(body):
    text = str(body or "")
    return _parse(text.splitlines()) if AUTO_REVIEW_MARKER in text else {}


def linked_issue_number(body, state=None):
    state = state or parse_auto_state(body)
    value = str(state.get("ISSUE") or "")
    if value.isdigit():
        return int(value)
    match = ISSUE_REF_RE.search(str(body or ""))
    return int(match.group(1)) if match else None


def lock_artifacts(changed_files):
    return sorted(p for p in (changed_files or []) if str(p).startswith(".automation-locks/"))


def high_risk_paths(changed_files, base_ref="master"):
    out = []
    if base_ref != "master":
        out.append(f"NON_MASTER_BASE:{base_ref}")
    for raw in changed_files or []:
        path = str(raw)
        name = path.rsplit("/", 1)[-1]
        if (path in RISK_EXACT or path.startswith((".github/workflows/", ".github/actions/", ".automation-locks/"))
                or path.startswith(("scripts/pr_lifecycle_", "scripts/github_pr_lifecycle_", "scripts/pr_review_merge_"))
                or DEPENDENCY_RE.fullmatch(name) or DOCKER_RE.fullmatch(name) or RISK_NAME_RE.search(path)):
            out.append(path)
    return sorted(set(out))


def high_risk_evidence_requirements(changed_files, base_ref="master"):
    risky = high_risk_paths(changed_files, base_ref)
    if not risky:
        return {"jobs": set(), "steps": set()}
    steps = set(RISK_STEPS)
    if any(p.startswith((".github/", "scripts/pr_lifecycle_", "scripts/github_pr_lifecycle_", "scripts/pr_review_merge_")) or RISK_NAME_RE.search(p) for p in risky):
        steps.update(LIFECYCLE_STEPS)
    return {"jobs": set(RISK_JOBS), "steps": steps}


def trusted_pr(pr):
    return (pr.get("state") == "open" and (pr.get("user") or {}).get("login") == TRUSTED_OWNER
            and ((pr.get("head") or {}).get("repo") or {}).get("full_name") == TRUSTED_REPOSITORY)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def promotion_decision(*, pr, run, current_base_sha, run_merge_parents, changed_files, issue_author=None):
    if not trusted_pr(pr): return Decision(False, "UNTRUSTED_PR")
    if not pr.get("draft"): return Decision(False, "PR_NOT_DRAFT")
    if run.get("name") != "Pre-merge Security Gate": return Decision(False, "WRONG_WORKFLOW_NAME")
    if run.get("path") != ".github/workflows/pre-merge-security-gate.yml": return Decision(False, "WRONG_WORKFLOW_PATH")
    if run.get("event") != "pull_request" or run.get("conclusion") != "success": return Decision(False, "GATE_NOT_SUCCESSFUL")
    head = (pr.get("head") or {}).get("sha")
    if run.get("head_sha") != head: return Decision(False, "STALE_GATE_HEAD")
    if list(run_merge_parents or []) != [current_base_sha, head]: return Decision(False, "STALE_GATE_BASE_OR_MERGE_REF")
    body = pr.get("body") or ""
    if AUTO_DEV_LOCK not in body: return Decision(False, "AUTO_DEV_LOCK_MISSING")
    state = parse_auto_state(body)
    if state.get("CHECKPOINT") != "READY_FOR_REVIEW_PREPARED": return Decision(False, "NOT_PREPARED_FOR_REVIEW")
    if state.get("LAST_HEAD") != head: return Decision(False, "AUTO_STATE_HEAD_STALE")
    if lock_artifacts(changed_files): return Decision(False, "LOCK_ARTIFACT_NOT_REMOVED")
    if linked_issue_number(body, state) is not None and issue_author != TRUSTED_OWNER: return Decision(False, "UNTRUSTED_LINKED_ISSUE")
    return Decision(True, "PROMOTE")


def verdict_binding_decision(*, pr, comment, current_base_sha, issue_author=None):
    if not trusted_pr(pr): return Decision(False, "UNTRUSTED_PR")
    if (comment.get("user") or {}).get("login") != TRUSTED_OWNER: return Decision(False, "UNTRUSTED_VERDICT_AUTHOR")
    parsed = parse_review_verdict(comment.get("body"))
    if not parsed: return Decision(False, "VERDICT_MARKER_MISSING")
    if parsed.get("HEAD_SHA") != (pr.get("head") or {}).get("sha"): return Decision(False, "STALE_VERDICT_HEAD")
    if parsed.get("BASE_SHA") != current_base_sha: return Decision(False, "STALE_VERDICT_BASE")
    if linked_issue_number(pr.get("body")) is not None and issue_author != TRUSTED_OWNER: return Decision(False, "UNTRUSTED_LINKED_ISSUE")
    verdict = parsed.get("VERDICT") or ""
    return Decision(bool(verdict), verdict or "VERDICT_MISSING")


def auto_merge_decision(*, pr, comment, current_base_sha, changed_files, exact_gate_valid, high_risk_checks_valid=False, issue_author=None):
    bound = verdict_binding_decision(pr=pr, comment=comment, current_base_sha=current_base_sha, issue_author=issue_author)
    if not bound.allowed: return bound
    if bound.reason != AUTO_MERGE_VERDICT: return Decision(False, f"VERDICT_{bound.reason}")
    if pr.get("draft"): return Decision(False, "PR_IS_DRAFT")
    if pr.get("mergeable") is not True: return Decision(False, "PR_NOT_MERGEABLE")
    if lock_artifacts(changed_files): return Decision(False, "LOCK_ARTIFACT_NOT_REMOVED")
    if not exact_gate_valid: return Decision(False, "EXACT_GATE_NOT_VALID")
    risky = high_risk_paths(changed_files, (pr.get("base") or {}).get("ref") or "")
    if risky and not high_risk_checks_valid: return Decision(False, "HIGH_RISK_CHECKS_NOT_VALID")
    return Decision(True, "MERGE_HIGH_RISK_VERIFIED" if risky else "MERGE")


def merged_parent_decision(validated_base_sha, head_sha, merged_parents):
    return Decision(list(merged_parents or []) == [validated_base_sha, head_sha], "MERGED_PARENTS_EXACT" if list(merged_parents or []) == [validated_base_sha, head_sha] else "POST_MERGE_BASE_OR_HEAD_RACE")


def should_close_issue_on_merge(pr):
    return str(parse_auto_state(pr.get("body")).get("ISSUE_CLOSE_ON_MERGE") or "").strip().lower() == "true"
