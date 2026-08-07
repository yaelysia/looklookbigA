# v1 稳定分支维护策略

`v1` 是 looklookbigA 对外复用的稳定发布分支，`master` 是持续开发分支。

## 分支职责

- `master`：接收新功能、修复和结构调整。代码仍按功能分支 → PR → CI → Review → 合并的流程进入 `master`。
- `v1`：只接收已经在 `master` 上通过完整验证的稳定版本，不自动跟随 `master`。
- `market-data`：仅保存主仓库自动生成的日 K 缓存和轻量盘中历史，不属于发布代码。

## 对外使用

希望稳定复用时，调用者应固定：

```yaml
jobs:
  quotes:
    uses: yaelysia/looklookbigA/.github/workflows/reusable-a-share-quotes.yml@v1
    with:
      source_ref: v1
```

希望测试最新开发版本时使用：

```yaml
jobs:
  quotes:
    uses: yaelysia/looklookbigA/.github/workflows/reusable-a-share-quotes.yml@master
    with:
      source_ref: master
```

`uses` 的 ref 和 `source_ref` 应保持一致，避免“工作流定义来自稳定版、实际执行引擎却来自开发版”这样的混合状态。

## 发布到 v1 的条件

一次 `master → v1` 晋升至少满足：

1. 目标功能已经通过功能 PR 合并到 `master`；
2. `Realtime A-share Quotes` 在合并后的 `master` 成功；
3. `Reusable Workflow Selftest` 成功；
4. live-price guard fault-injection tests 成功；
5. 如果修改了 snapshot schema、配置格式或 reusable workflow，README 已同步更新；
6. 创建 `master → v1` 的发布 PR，检查差异后再合并。

`v1` 不采用“master 每次提交自动同步”的方式，以免实验性功能在未经验证时影响外部使用者。

## 发布 PR 的合并方式

功能 PR 进入 `master` 可以使用 squash merge。

`master → v1` 的发布 PR 应使用普通 **merge commit**，不要 squash。这样 `v1` 会保留 `master` 的提交祖先关系，下一次发布时 GitHub 可以准确计算仅新增的差异。

## v1 自检

`.github/workflows/v1-smoke.yml` 会在 `v1` 收到新提交时运行：

- live current-price guard 故障注入测试；
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
