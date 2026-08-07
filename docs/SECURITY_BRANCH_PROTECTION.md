# GitHub 分支保护配置

Issue #22 的目标不是只禁止 direct push，还要保证 `master` / `v1` 的 Pull Request 在合并前必须通过稳定、始终存在的安全检查。

## 仓库 Ruleset

仓库使用 repository ruleset：

```text
Protect master and v1
```

目标分支：

```text
master
v1
```

基础规则：

- 禁止直接修改受保护分支，变更通过 Pull Request 进入；
- 禁止删除受保护分支；
- 禁止 force push / non-fast-forward 更新；
- bypass list 保持为空；
- Required approvals 当前可以为 0，适配单人维护仓库。

## 必须强制的 pre-merge check

仓库提供：

```text
.github/workflows/pre-merge-security-gate.yml
```

它在所有以 `master` 或 `v1` 为目标分支的 Pull Request 上运行，并且**没有 path filter**。因此无论 PR 只改文档、workflow、脚本还是 Worker，都一定会产生最终检查：

```text
pre-merge-security-gate
```

该 gate 只有在下面两组任务都成功时才通过：

1. Safety tests：live-price guard、quote resilience、watchlist/config 攻击边界、Action SHA / workflow 安全、HTTPS-only、Worker abuse-protection；
2. Reusable smoke：使用小型观察列表真实执行 reusable workflow、校验 engine revision、生成 snapshot 并上传 artifact。

Ruleset 中应开启 **Require status checks to pass**，并把 `pre-merge-security-gate` 配置成 Required status check。不要把只在 `push` 后运行的 `v1-smoke` 作为 pre-merge required check，也不要使用带 path filter 的 workflow 作为唯一 required check，否则某些 PR 可能永远等待一个不会产生的状态。

## master

- 只能通过 Pull Request 更新；
- `pre-merge-security-gate` 必须成功后才能合并；
- 新功能/安全修复按 feature/security branch → PR → gate → master 的流程进入。

## v1

- 只能通过 Pull Request 更新；
- `pre-merge-security-gate` 必须成功后才能合并；
- 只通过已经在 master 验证完成的 `master → v1` 发布 PR 晋升；
- `v1-smoke.yml` 继续作为**合并后的稳定分支验证**，不是 pre-merge gate。

## 推荐发布顺序

```text
feature / security branch
        ↓ PR
pre-merge-security-gate
        ↓ PASS
      master
        ↓ release PR
pre-merge-security-gate
        ↓ PASS
        v1
        ↓
     v1-smoke
```

只有 Ruleset 的 `required_status_checks` 真正包含 `pre-merge-security-gate` 后，Issue #22 才算完整关闭。
