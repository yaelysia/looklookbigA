# v1 稳定分支维护策略

`v1` 是 looklookbigA 对外复用的稳定发布分支，`master` 是持续开发分支。

## 分支职责

- `master`：接收新功能、修复和结构调整。代码仍按功能分支 → PR → CI → Review → 合并的流程进入 `master`。
- `v1`：只接收已经在 `master` 上通过完整验证的稳定版本，不自动跟随 `master`。
- `market-data`：仅保存主仓库自动生成的日 K 缓存和轻量盘中历史，不属于发布代码。

## 对外使用

希望稳定复用时，调用者固定 reusable workflow 自身的版本即可：

```yaml
jobs:
  quotes:
    uses: yaelysia/looklookbigA/.github/workflows/reusable-a-share-quotes.yml@v1
```

workflow 内部会把实际执行的 looklookbigA engine 绑定到该 reusable workflow 对应的精确 commit SHA，不再默认 checkout `master`，也不需要调用者额外传 `source_ref: v1`。

希望测试最新开发版本时使用：

```yaml
jobs:
  quotes:
    uses: yaelysia/looklookbigA/.github/workflows/reusable-a-share-quotes.yml@master
```

这里 `master` 本身仍是可变开发入口，因此只适合测试。需要最高不可变性时，应把 `uses:` 直接固定到完整 40 位 commit SHA。

`source_ref` 仍保留为显式 engine override，但仅接受完整 40 位 commit SHA；不再接受 `master`、`v1` 或其他可变 branch/tag 名称。未传时以 `job.workflow_sha` 为准，checkout 后还会再次验证实际 engine SHA。

## reusable workflow 安全边界

对外调用输入按不可信数据处理：

- `config_path` 必须位于 caller repository 内，拒绝绝对路径、`../` 目录逃逸和 symlink 逃逸；
- 配置 JSON 最大 32 KiB；
- `max_total_codes` 硬上限为 50，并对 detail/light/group 合并后的唯一股票总数统一计数；
- 单 group、group member 数量和原始 code entry 数量均有硬限制；
- 重复股票代码和非法 group id 会被直接拒绝，而不是静默截断；
- 第三方 GitHub Actions 使用完整 commit SHA 固定，并通过 Dependabot 跟踪上游安全更新；
- 腾讯 quote fallback 只允许 HTTPS，HTTPS 失败时不会降级到明文 HTTP。

这些规则由 `scripts/test_config_security.py` 和 `scripts/test_workflow_security.py` 持续回归。

## 发布到 v1 的条件

一次 `master → v1` 晋升至少满足：

1. 目标功能已经通过功能 PR 合并到 `master`；
2. `Realtime A-share Quotes` 在合并后的 `master` 成功；
3. `Reusable Workflow Selftest` 成功；
4. live-price guard、quote resilience、config boundary、workflow supply-chain / HTTPS transport 等安全测试成功；
5. Web 行情入口涉及安全逻辑时，public quote abuse-protection tests 成功；
6. 如果修改了 snapshot schema、配置格式或 reusable workflow，README 已同步更新；
7. 创建 `master → v1` 的发布 PR，检查差异后再合并。

`v1` 不采用“master 每次提交自动同步”的方式，以免实验性功能在未经验证时影响外部使用者。

## 分支保护要求

仓库设置层建议同时启用：

- `master`：禁止直接 push，要求通过 PR，并要求核心 CI / safety checks 通过后才能合并；
- `v1`：禁止直接 push，只允许通过经过验证的发布 PR 晋升；
- 发布顺序保持 feature/security branch → `master` → `v1`。

这些属于 GitHub repository rules / branch protection 设置，不由仓库内 YAML 文件本身强制实现。仓库代码和 CI 负责提供可作为 required checks 的验证任务。

## 发布 PR 的合并方式

功能 PR 进入 `master` 可以使用 squash merge。

`master → v1` 的发布 PR 应使用普通 **merge commit**，不要 squash。这样 `v1` 会保留 `master` 的提交祖先关系，下一次发布时 GitHub 可以准确计算仅新增的差异。

## v1 自检

`.github/workflows/v1-smoke.yml` 会在 `v1` 收到新提交时运行：

- live current-price guard 故障注入测试；
- quote resilience、配置边界、Action SHA / HTTPS transport 测试；
- Web 公共行情接口滥用防护测试；
- 通过 reusable workflow 自己运行一组小型 A 股观察列表；
- 生成 `v1-smoke-snapshot` artifact。

因此每次稳定版晋升都有单独的 CI 记录。

## 兼容性原则

在 `v1` 上优先保持：

- `config/quote_watchlist.json` 的主要字段兼容；
- reusable workflow 现有输入参数兼容；
- `snapshot.json` 已有字段尽量只增不删；
- realtime/current price 永远不得从历史 cache / snapshot / daily K 回填；
- 必须进行破坏性修改时，应考虑新的稳定主版本分支，而不是直接破坏 `v1` 使用者。
