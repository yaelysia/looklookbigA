# GitHub 分支保护待配置项

仓库代码侧的安全硬化完成后，仍需在 GitHub repository rules / branch protection 中启用以下设置，才能完成 issue #22。

## master

- 禁止直接 push，变更通过 Pull Request 进入；
- 要求 Reusable Workflow Selftest 等核心检查通过后才能合并；
- 管理员是否允许绕过按仓库维护策略决定，但稳定发布流程不应依赖绕过。

## v1

- 禁止直接 push；
- 只通过已经在 master 验证完成的 `master → v1` 发布 PR 晋升；
- 保留 `v1-smoke.yml` 作为晋升后的稳定分支验证。

## 推荐发布顺序

```text
feature / security branch
        ↓ PR + CI
      master
        ↓ release PR
        v1
```

这些规则属于 GitHub 仓库设置，不能由普通 workflow YAML 自己阻止直接 push。配置完成后再关闭 issue #22。
