# GitHub-native PR lifecycle gates

`looklookbigA` keeps semantic development and review decisions in ChatGPT Scheduled Tasks, but moves deterministic PR lifecycle mutations into workflows that execute trusted code from the repository default branch.

This split removes Draft/Ready/merge actions from the Scheduled Task critical path while retaining fail-closed identity, CI and trust checks.

## Responsibility split

```text
Scheduled Developer
  -> implement / test / fix
  -> remove .automation-locks/** from final PR tree
  -> write CHECKPOINT=READY_FOR_REVIEW_PREPARED
  -> stop

GitHub PR Promotion Gate
  -> exact head/base + successful Pre-merge Security Gate
  -> trusted same-repo PR / Issue
  -> no work-lock artifact
  -> Draft -> Ready

Scheduled Reviewer
  -> independent review only
  -> top-level structured verdict comment
     CHANGES_REQUIRED | WAITING_* | MANUAL_GATE_REQUIRED | PASS_AUTOMERGE
  -> never directly merges

GitHub PR Review Merge Gate
  -> stale verdict / head / base rejection
  -> CHANGES_REQUIRED: Ready -> Draft
  -> PASS_AUTOMERGE: atomic latest-base prerequisite + exact current gate + low-risk path policy + normal exact-head merge
  -> optional linked-Issue close only when ISSUE_CLOSE_ON_MERGE=true
  -> dispatch Native Merge Postcheck
```

## Promotion contract

A Draft is eligible only when all of the following are true:

- repository, PR author and head repository are exactly `yaelysia/looklookbigA` / `yaelysia`;
- PR body contains `<!-- looklookbigA-auto-dev-lock -->`;
- machine state has `CHECKPOINT=READY_FOR_REVIEW_PREPARED` and `LAST_HEAD=<current head>`;
- linked machine-state Issue, when present, is authored by `yaelysia`;
- the exact current head has a successful `Pre-merge Security Gate` run;
- the merge-ref commit validated by that run has parents exactly `[current base SHA, current head SHA]`;
- final changed files contain no `.automation-locks/**`.

The Promotion Gate is triggered both by `workflow_run` completion and trusted `pull_request_target` metadata/synchronize events. `pull_request_target` never checks out PR code: it explicitly checks out the repository default branch with credentials persistence disabled, then re-fetches the candidate PR through the GitHub API.

## Reviewer verdict contract

The reviewer must use a top-level PR comment with:

```text
<!-- looklookbigA-auto-review -->
HEAD_SHA=<current head>
BASE_SHA=<current base>
VERDICT=<value>
REASON_CODES=<csv>
```

Only comments authored by `yaelysia` are trusted. Any head/base drift makes the verdict stale.

`PASS_AUTOMERGE` means only that independent review found no blocker and the PR is eligible to enter the native auto-merge policy. It is not itself merge authority; the GitHub gate re-validates every condition.

## Native auto-merge exclusions

Native auto-merge is intentionally unavailable for non-`master` bases and for high-risk paths, including:

- `.github/workflows/**`, `.github/actions/**`, CODEOWNERS and Dependabot control files;
- lifecycle-gate implementation files themselves;
- security/auth/credential/secret/permission-related paths;
- release/stable-v1, ruleset/branch-protection and deployment control paths;
- dependency manifests/lockfiles/requirements and package-manager control files;
- Docker/base-image and compose control files;
- `.automation-locks/**`.

These changes require `MANUAL_GATE_REQUIRED`. The gate independently enforces this even if a stale or incorrect reviewer were to emit `PASS_AUTOMERGE`.

## Exact merge binding

Before a native merge, the gate re-fetches:

- PR open/Ready state and mergeability;
- current head and current base branch SHA;
- final changed files;
- linked Issue author;
- structured verdict author/head/base;
- an exact successful Pre-merge Security Gate whose merge-ref parents are the current base and head.

The REST merge endpoint can atomically bind `sha=<current head>`, but it has no expected-base SHA parameter. A preflight base re-read alone is therefore insufficient: another merge can advance `master` after that read and before the merge request.

For every `PASS_AUTOMERGE` event, `scripts/pr_lifecycle_atomic_base.py` runs before the merge gate and fails closed unless the exact target base ref is covered by an **active ruleset that explicitly names that ref**, requires the `pre-merge-security-gate` status check, and has `strict_required_status_checks_policy=true`. When GitHub exposes bypass information, a bypassable ruleset is not accepted as this prerequisite. Under that strict policy, a base move makes the PR out-of-date and GitHub rejects the merge rather than allowing the newer, unvalidated base to land.

The existing immediate head/base re-read remains a defense-in-depth check. The returned merge commit parent verification remains an audit/reconciliation check only; it is not treated as the base-CAS guarantee because it runs after the merge is irreversible. No force update, admin bypass or ruleset bypass is used.

At the time this contract was added, repository ruleset `Protect master and v1` had `strict_required_status_checks_policy=false`. Therefore native `PASS_AUTOMERGE` intentionally remains disabled until that repository setting is manually changed to strict (or an equivalent atomic latest-base mechanism is introduced). `CHANGES_REQUIRED` demotion and other non-merge verdict handling continue to work without that prerequisite.

## GITHUB_TOKEN event suppression and postcheck

A merge performed with Actions `GITHUB_TOKEN` may not create ordinary downstream workflow events. Native auto-merge therefore explicitly creates a `repository_dispatch` event after a successful merge.

`Native Merge Postcheck` checks out the exact returned merge SHA, first verifies that the merge parents still match the validated base/head pair carried in the dispatch payload, then replays the pre-merge safety command catalog through `scripts/pr_native_postcheck.py` and runs an exact-SHA `INTRADAY_FAST` reusable smoke. This keeps native merges from silently losing the validation that push-triggered repository workflows historically supplied.

## Linked Issue closure

Issue closure is explicit, not inferred. Developer machine state may set:

```text
ISSUE_CLOSE_ON_MERGE=true
```

Only then, after a successful native merge, may the gate close the linked Issue, and only if that Issue is authored by `yaelysia`. Multi-stage Issues such as stable-v1 promotions must use `false`.

## Bootstrap rule

Changes to these workflows/scripts are themselves trust-boundary changes and therefore cannot be auto-merged by the gate they define. Their implementation PR must be reviewed and merged through the existing manual gate.
