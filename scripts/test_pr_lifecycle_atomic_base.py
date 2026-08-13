import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("pr_lifecycle_atomic_base.py")
SPEC = importlib.util.spec_from_file_location("pr_lifecycle_atomic_base", MODULE_PATH)
atomic = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(atomic)


def _ruleset(*, strict=False, include=("refs/heads/master", "refs/heads/v1"), contexts=("pre-merge-security-gate",), enforcement="active", bypass=None, can_bypass="never", pull_request=True, non_fast_forward=True):
    rules = []
    if non_fast_forward:
        rules.append({"type": "non_fast_forward"})
    if pull_request:
        rules.append({"type": "pull_request", "parameters": {"required_approving_review_count": 0}})
    rules.append({"type": "required_status_checks", "parameters": {
        "strict_required_status_checks_policy": strict,
        "required_status_checks": [{"context": c, "integration_id": 15368} for c in contexts],
    }})
    return {
        "id": 20545971,
        "target": "branch",
        "enforcement": enforcement,
        "conditions": {"ref_name": {"include": list(include), "exclude": []}},
        "rules": rules,
        "bypass_actors": list(bypass or []),
        "current_user_can_bypass": can_bypass,
    }


def test_strict_prefers_merge_api():
    assert atomic.atomic_merge_transport([_ruleset(strict=True)], "master") == atomic.STRICT_MERGE_API


def test_non_strict_uses_verified_merge_ref_fast_forward():
    ruleset = _ruleset(strict=False)
    assert atomic.atomic_merge_transport([ruleset], "master") == atomic.VERIFIED_MERGE_REF_FF
    assert atomic.atomic_latest_base_prerequisite([ruleset], "master") is True


def test_fail_closed_without_required_transport_properties():
    bad = [
        _ruleset(contexts=("other-check",)),
        _ruleset(include=("refs/heads/v1",)),
        _ruleset(enforcement="evaluate"),
        _ruleset(bypass=[{"actor_id": 1, "actor_type": "Integration"}]),
        _ruleset(can_bypass="always"),
        _ruleset(pull_request=False),
        _ruleset(non_fast_forward=False),
    ]
    for ruleset in bad:
        assert atomic.atomic_merge_transport([ruleset], "master") == ""


def test_review_workflow_uses_new_atomic_gate():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pr-review-merge-gate.yml").read_text(encoding="utf-8")
    prerequisite = "python3 scripts/pr_lifecycle_atomic_base.py"
    gate = "python3 scripts/pr_review_merge_gate.py"
    assert prerequisite in workflow and gate in workflow
    assert workflow.index(prerequisite) < workflow.index(gate)
    assert "python3 scripts/github_pr_lifecycle_gate.py review" not in workflow


def test_premerge_runs_atomic_regressions():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pre-merge-security-gate.yml").read_text(encoding="utf-8")
    assert "python3 scripts/test_pr_lifecycle_atomic_base.py" in workflow


def main():
    tests = [test_strict_prefers_merge_api, test_non_strict_uses_verified_merge_ref_fast_forward, test_fail_closed_without_required_transport_properties, test_review_workflow_uses_new_atomic_gate, test_premerge_runs_atomic_regressions]
    for test in tests:
        test(); print(f"PASS {test.__name__}")
    print(f"PR_LIFECYCLE_ATOMIC_BASE_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
