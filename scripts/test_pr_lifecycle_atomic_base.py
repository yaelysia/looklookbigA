import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("pr_lifecycle_atomic_base.py")
SPEC = importlib.util.spec_from_file_location("pr_lifecycle_atomic_base", MODULE_PATH)
atomic = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(atomic)


def _ruleset(
    *,
    strict=True,
    include=("refs/heads/master", "refs/heads/v1"),
    exclude=(),
    contexts=("pre-merge-security-gate",),
    enforcement="active",
    target="branch",
    bypass_actors=None,
    current_user_can_bypass="never",
):
    return {
        "id": 20545971,
        "name": "Protect master and v1",
        "target": target,
        "enforcement": enforcement,
        "conditions": {"ref_name": {"include": list(include), "exclude": list(exclude)}},
        "rules": [{"type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": strict,
            "required_status_checks": [{"context": context, "integration_id": 15368} for context in contexts],
        }}],
        "bypass_actors": list(bypass_actors or []),
        "current_user_can_bypass": current_user_can_bypass,
    }


def test_accepts_exact_active_strict_required_gate():
    assert atomic.atomic_latest_base_prerequisite([_ruleset()], "master") is True


def test_rejects_non_strict_missing_context_and_wrong_ref():
    assert atomic.atomic_latest_base_prerequisite([_ruleset(strict=False)], "master") is False
    assert atomic.atomic_latest_base_prerequisite([_ruleset(contexts=("some-other-check",))], "master") is False
    assert atomic.atomic_latest_base_prerequisite([_ruleset(include=("refs/heads/v1",))], "master") is False


def test_rejects_inactive_excluded_or_bypassable_rulesets():
    assert atomic.atomic_latest_base_prerequisite([_ruleset(enforcement="evaluate")], "master") is False
    assert atomic.atomic_latest_base_prerequisite([_ruleset(exclude=("refs/heads/master",))], "master") is False
    assert atomic.atomic_latest_base_prerequisite([_ruleset(bypass_actors=[{"actor_id": 1, "actor_type": "Integration"}])], "master") is False
    assert atomic.atomic_latest_base_prerequisite([_ruleset(current_user_can_bypass="always")], "master") is False


def test_review_workflow_enforces_prerequisite_before_merge_gate():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pr-review-merge-gate.yml").read_text(encoding="utf-8")
    prerequisite = "python3 scripts/pr_lifecycle_atomic_base.py"
    gate = "python3 scripts/github_pr_lifecycle_gate.py review"
    assert prerequisite in workflow
    assert gate in workflow
    assert workflow.index(prerequisite) < workflow.index(gate)
    assert "GITHUB_TOKEN: ${{ github.token }}" in workflow


def test_premerge_runs_atomic_base_regressions():
    premerge = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pre-merge-security-gate.yml").read_text(encoding="utf-8")
    assert "Run PR atomic latest-base prerequisite tests" in premerge
    assert "python3 scripts/test_pr_lifecycle_atomic_base.py" in premerge


def main():
    tests = [
        test_accepts_exact_active_strict_required_gate,
        test_rejects_non_strict_missing_context_and_wrong_ref,
        test_rejects_inactive_excluded_or_bypassable_rulesets,
        test_review_workflow_enforces_prerequisite_before_merge_gate,
        test_premerge_runs_atomic_base_regressions,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PR_LIFECYCLE_ATOMIC_BASE_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
