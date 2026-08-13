# GitHub-native PR lifecycle gates

The default branch owns deterministic PR lifecycle actions. Scheduled Developer writes code/tests/checkpoints; Scheduled Reviewer writes exact structured verdicts.

## Lifecycle

Developer finishes work, removes `.automation-locks/**`, and writes `CHECKPOINT=READY_FOR_REVIEW_PREPARED` bound to the current head. Promotion verifies the trusted same-repo PR and linked Issue, current head/base, exact successful `Pre-merge Security Gate`, exact merge-ref parents, and absence of work-lock artifacts before Draft -> Ready.

Reviewer verdict comments contain the marker plus exact `HEAD_SHA`, `BASE_SHA`, `VERDICT`, and `REASON_CODES`. Any identity drift makes a verdict stale. `PASS_AUTOMERGE` is evidence only; the merge gate independently rechecks every predicate.

## Risk policy

High-risk paths are not an unconditional manual gate. Workflow/action, lifecycle/security, ruleset, dependency, Docker, release/deploy and non-master changes require stronger evidence from the exact successful gate run. Required baseline jobs are `Safety tests`, `Reusable smoke`, and `pre-merge-security-gate`; lifecycle-sensitive changes also require the workflow transport test and lifecycle/atomic-merge regression steps. Missing required evidence rejects auto-merge.

## Atomic merge transport

The merge gate accepts only an active ruleset covering the exact target branch, requiring `pre-merge-security-gate`, with no available bypass path. It chooses one verified server-side transport:

- When required-status checks are strict, use the normal PR merge API bound to the exact head.
- When strict mode is off but the ruleset requires pull-request changes and rejects non-fast-forward updates, use the exact GitHub synthetic merge commit validated by the successful gate. Its parents must equal `[current base, current head]`. Re-read PR/head/base immediately before writing, reconfirm the exact gate, then move the target ref to that verified merge commit with `force=false`.

A concurrent incompatible base move is rejected by the server-side fast-forward update. The gate does not force-update, rebase, or construct an unverified merge tree. If neither transport is proven, merge fails closed.

The current `Protect master and v1` configuration can therefore use the verified merge-ref fast-forward transport while strict status checks remain off; ordinary automation does not depend on a later human settings change.

## Postcheck

After merge, parent identity is verified, `looklookbiga-native-merge-postcheck` is dispatched, and safety tests run against the exact merged SHA. Linked Issues close only when `ISSUE_CLOSE_ON_MERGE=true` and the Issue author is trusted.

## Bootstrap

PR #69 is a one-time bootstrap because the new default-branch gates cannot govern themselves before landing. Its temporary bootstrap authority uses the same exact head/base/CI and atomic-merge requirements. After #69 enters `master`, the exception ends and ordinary lifecycle actions remain GitHub-native.
