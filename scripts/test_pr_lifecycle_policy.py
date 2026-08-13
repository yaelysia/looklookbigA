import pr_lifecycle_policy as policy


HEAD = "1" * 40
BASE = "2" * 40


def _body(checkpoint="READY_FOR_REVIEW_PREPARED", head=HEAD, close="true"):
    return f"""Refs #68

<!-- looklookbigA-auto-dev-lock -->
<!-- looklookbigA-auto-state
STATE=VALIDATING
ISSUE=68
PR=69
CHECKPOINT={checkpoint}
LAST_HEAD={head}
ISSUE_CLOSE_ON_MERGE={close}
NEXT_ACTION=await native gate
-->
"""


def _pr(*, draft=True, mergeable=True, head=HEAD, base_ref="master", body=None, author="yaelysia", head_repo="yaelysia/looklookbigA"):
    return {
        "number": 69,
        "state": "open",
        "draft": draft,
        "mergeable": mergeable,
        "body": _body() if body is None else body,
        "user": {"login": author},
        "head": {"sha": head, "repo": {"full_name": head_repo}},
        "base": {"ref": base_ref},
    }


def _run(*, head=HEAD, conclusion="success"):
    return {
        "name": "Pre-merge Security Gate",
        "path": ".github/workflows/pre-merge-security-gate.yml",
        "event": "pull_request",
        "conclusion": conclusion,
        "head_sha": head,
    }


def _comment(verdict="PASS_AUTOMERGE", head=HEAD, base=BASE, author="yaelysia"):
    return {
        "user": {"login": author},
        "body": f"""<!-- looklookbigA-auto-review -->
HEAD_SHA={head}
BASE_SHA={base}
VERDICT={verdict}
REASON_CODES=NONE
""",
    }


def test_parse_state_and_review_binding():
    state = policy.parse_auto_state(_body())
    assert state["ISSUE"] == "68"
    assert state["CHECKPOINT"] == "READY_FOR_REVIEW_PREPARED"
    assert state["LAST_HEAD"] == HEAD
    parsed = policy.parse_review_verdict(_comment()["body"])
    assert parsed["VERDICT"] == "PASS_AUTOMERGE"
    assert parsed["HEAD_SHA"] == HEAD


def test_promotion_accepts_exact_prepared_candidate():
    decision = policy.promotion_decision(
        pr=_pr(),
        run=_run(),
        current_base_sha=BASE,
        run_merge_parents=[BASE, HEAD],
        changed_files=["scripts/example.py", "README.md"],
        issue_author="yaelysia",
    )
    assert decision == policy.Decision(True, "PROMOTE")


def test_promotion_rejects_stale_base_head_and_work_lock():
    stale_base = policy.promotion_decision(
        pr=_pr(),
        run=_run(),
        current_base_sha=BASE,
        run_merge_parents=["3" * 40, HEAD],
        changed_files=["scripts/example.py"],
        issue_author="yaelysia",
    )
    assert stale_base.reason == "STALE_GATE_BASE_OR_MERGE_REF"

    stale_head = policy.promotion_decision(
        pr=_pr(),
        run=_run(head="4" * 40),
        current_base_sha=BASE,
        run_merge_parents=[BASE, HEAD],
        changed_files=["scripts/example.py"],
        issue_author="yaelysia",
    )
    assert stale_head.reason == "STALE_GATE_HEAD"

    lock = policy.promotion_decision(
        pr=_pr(),
        run=_run(),
        current_base_sha=BASE,
        run_merge_parents=[BASE, HEAD],
        changed_files=[".automation-locks/issue-68.json"],
        issue_author="yaelysia",
    )
    assert lock.reason == "LOCK_ARTIFACT_NOT_REMOVED"


def test_promotion_rejects_external_or_untrusted_issue():
    external = policy.promotion_decision(
        pr=_pr(head_repo="someone/fork"),
        run=_run(),
        current_base_sha=BASE,
        run_merge_parents=[BASE, HEAD],
        changed_files=[],
        issue_author="yaelysia",
    )
    assert external.reason == "UNTRUSTED_PR"

    issue = policy.promotion_decision(
        pr=_pr(),
        run=_run(),
        current_base_sha=BASE,
        run_merge_parents=[BASE, HEAD],
        changed_files=[],
        issue_author="someone",
    )
    assert issue.reason == "UNTRUSTED_LINKED_ISSUE"


def test_review_verdict_is_exact_head_and_base_bound():
    stale_head = policy.verdict_binding_decision(
        pr=_pr(draft=False),
        comment=_comment(head="4" * 40),
        current_base_sha=BASE,
        issue_author="yaelysia",
    )
    assert stale_head.reason == "STALE_VERDICT_HEAD"

    stale_base = policy.verdict_binding_decision(
        pr=_pr(draft=False),
        comment=_comment(base="5" * 40),
        current_base_sha=BASE,
        issue_author="yaelysia",
    )
    assert stale_base.reason == "STALE_VERDICT_BASE"

    untrusted = policy.verdict_binding_decision(
        pr=_pr(draft=False),
        comment=_comment(author="someone"),
        current_base_sha=BASE,
        issue_author="yaelysia",
    )
    assert untrusted.reason == "UNTRUSTED_VERDICT_AUTHOR"


def test_auto_merge_accepts_only_low_risk_exact_gate_pass():
    decision = policy.auto_merge_decision(
        pr=_pr(draft=False),
        comment=_comment(),
        current_base_sha=BASE,
        changed_files=["scripts/valuation_context.py", "docs/VALUATION.md"],
        exact_gate_valid=True,
        issue_author="yaelysia",
    )
    assert decision == policy.Decision(True, "MERGE")


def test_auto_merge_fail_closes_high_risk_paths_and_non_master():
    for path in (
        ".github/workflows/pre-merge-security-gate.yml",
        ".github/actions/local/action.yml",
        "scripts/transport_security.py",
        "requirements-event-facts.txt",
        "Dockerfile",
        "scripts/pr_lifecycle_policy.py",
        "docs/STABLE_V1.md",
        "scripts/release_gate.py",
        "infra/deploy-prod.yml",
        "requirements-dev.txt",
        "docker-compose.yml",
        "infra/ruleset_policy.json",
        "scripts/branch_protection.py",
    ):
        decision = policy.auto_merge_decision(
            pr=_pr(draft=False),
            comment=_comment(),
            current_base_sha=BASE,
            changed_files=[path],
            exact_gate_valid=True,
            issue_author="yaelysia",
        )
        assert decision.reason == "HIGH_RISK_MANUAL_GATE", path

    non_master = policy.auto_merge_decision(
        pr=_pr(draft=False, base_ref="v1"),
        comment=_comment(),
        current_base_sha=BASE,
        changed_files=["scripts/example.py"],
        exact_gate_valid=True,
        issue_author="yaelysia",
    )
    assert non_master.reason == "HIGH_RISK_MANUAL_GATE"


def test_auto_merge_requires_ready_mergeable_clean_and_exact_gate():
    draft = policy.auto_merge_decision(
        pr=_pr(draft=True),
        comment=_comment(),
        current_base_sha=BASE,
        changed_files=["scripts/example.py"],
        exact_gate_valid=True,
        issue_author="yaelysia",
    )
    assert draft.reason == "PR_IS_DRAFT"

    dirty = policy.auto_merge_decision(
        pr=_pr(draft=False, mergeable=False),
        comment=_comment(),
        current_base_sha=BASE,
        changed_files=["scripts/example.py"],
        exact_gate_valid=True,
        issue_author="yaelysia",
    )
    assert dirty.reason == "PR_NOT_MERGEABLE"

    no_gate = policy.auto_merge_decision(
        pr=_pr(draft=False),
        comment=_comment(),
        current_base_sha=BASE,
        changed_files=["scripts/example.py"],
        exact_gate_valid=False,
        issue_author="yaelysia",
    )
    assert no_gate.reason == "EXACT_GATE_NOT_VALID"

    lock = policy.auto_merge_decision(
        pr=_pr(draft=False),
        comment=_comment(),
        current_base_sha=BASE,
        changed_files=[".automation-locks/issue-68.json"],
        exact_gate_valid=True,
        issue_author="yaelysia",
    )
    assert lock.reason == "LOCK_ARTIFACT_NOT_REMOVED"


def test_non_pass_verdict_never_merges_and_issue_close_is_explicit():
    decision = policy.auto_merge_decision(
        pr=_pr(draft=False),
        comment=_comment(verdict="MANUAL_GATE_REQUIRED"),
        current_base_sha=BASE,
        changed_files=["scripts/example.py"],
        exact_gate_valid=True,
        issue_author="yaelysia",
    )
    assert decision.reason == "VERDICT_MANUAL_GATE_REQUIRED"
    assert policy.should_close_issue_on_merge(_pr()) is True
    assert policy.should_close_issue_on_merge(_pr(body=_body(close="false"))) is False
    assert policy.merged_parent_decision(BASE, HEAD, [BASE, HEAD]).allowed is True
    assert policy.merged_parent_decision(BASE, HEAD, ["9" * 40, HEAD]).reason == "POST_MERGE_BASE_OR_HEAD_RACE"


def test_native_workflow_contract_is_default_branch_fail_closed():
    from pathlib import Path

    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    promotion = (workflows / "pr-promotion-gate.yml").read_text(encoding="utf-8")
    review = (workflows / "pr-review-merge-gate.yml").read_text(encoding="utf-8")
    postcheck = (workflows / "pr-native-postcheck.yml").read_text(encoding="utf-8")
    premerge = (workflows / "pre-merge-security-gate.yml").read_text(encoding="utf-8")

    assert "workflow_run:" in promotion
    assert "pull_request_target:" in promotion
    assert "Pre-merge Security Gate" in promotion
    assert "ref: ${{ github.event.repository.default_branch }}" in promotion
    assert "persist-credentials: false" in promotion
    assert "pull-requests: write" in promotion
    assert "contents: read" in promotion
    assert "python3 scripts/github_pr_lifecycle_gate.py promote" in promotion

    assert "issue_comment:" in review
    assert "pull-requests: write" in review
    assert "contents: write" in review
    assert "issues: write" in review
    assert "ref: ${{ github.event.repository.default_branch }}" in review
    assert "persist-credentials: false" in review
    assert "python3 scripts/github_pr_lifecycle_gate.py review" in review
    assert "group: pr-review-merge-${{ github.repository_id }}" in review

    assert "repository_dispatch:" in postcheck
    assert "looklookbiga-native-merge-postcheck" in postcheck
    assert "ref: ${{ github.event.client_payload.merged_sha }}" in postcheck
    assert "python3 scripts/pr_native_postcheck.py" in postcheck
    assert "source_ref: ${{ github.event.client_payload.merged_sha }}" in postcheck
    assert "execution_mode: INTRADAY_FAST" in postcheck
    assert "EXPECTED_BASE_SHA: ${{ github.event.client_payload.validated_base_sha }}" in postcheck
    assert "EXPECTED_HEAD_SHA: ${{ github.event.client_payload.head_sha }}" in postcheck

    assert "Run PR lifecycle policy tests" in premerge
    assert "python3 scripts/test_pr_lifecycle_policy.py" in premerge

    for text in (promotion, review, postcheck):
        for line in text.splitlines():
            if "uses: actions/checkout@" in line:
                ref = line.split("actions/checkout@", 1)[1].split()[0]
                assert len(ref) == 40 and all(ch in "0123456789abcdef" for ch in ref)


def main():
    tests = [
        test_parse_state_and_review_binding,
        test_promotion_accepts_exact_prepared_candidate,
        test_promotion_rejects_stale_base_head_and_work_lock,
        test_promotion_rejects_external_or_untrusted_issue,
        test_review_verdict_is_exact_head_and_base_bound,
        test_auto_merge_accepts_only_low_risk_exact_gate_pass,
        test_auto_merge_fail_closes_high_risk_paths_and_non_master,
        test_auto_merge_requires_ready_mergeable_clean_and_exact_gate,
        test_non_pass_verdict_never_merges_and_issue_close_is_explicit,
        test_native_workflow_contract_is_default_branch_fail_closed,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PR_LIFECYCLE_POLICY_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
