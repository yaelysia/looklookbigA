# Observation、历史持久化与分钟回放合同

历史链由三个层次组成：每次运行的 observation 身份、不可回退的历史树，以及按交易日/股票保存的完整分钟记录。

## Observation 身份与基线

CI 中每份 snapshot 使用以下四元组标识一次观察：

```text
(runner_time, run_id, run_attempt, head_sha)
```

字段位于顶层 `observation`，归档 manifest 同时保存 `latest_run_id`、`latest_run_attempt`、`latest_head_sha` 和 `latest_observation`。`changes_since_previous.baseline` 同时披露前后 observation，便于审计比较来源。

rerun 时，基线解析优先从同一 `run_id` 中逆序寻找最近的成功 attempt；没有成功的较早 attempt 时，才退回上一条独立成功 run。`scripts/history_artifact.py` 会检查 artifact 内部 manifest 和 snapshot，只接受与目标 run、attempt、head SHA 完全一致且唯一的 `market-history-state`。

首次升级时允许读取 schema-v2 旧 artifact：run/attempt 必须精确匹配，artifact listing 的 run/head SHA 也必须匹配；该旧 snapshot 会明确标记为缺少 observation 的低质量基线，不能获得分钟 replay 资格。新产物一律使用完整四元组。

## 单调持久化

`scripts/history_continuity.py` 使用文件锁串行 writer，在暂存树完成合并与校验后再提交：

- `snapshots/` 是不可变归档；同路径不同内容直接失败；
- 迟到的旧 run 可以补入缺失归档，但不能回退 `manifest.json` 的 latest 指针；
- 相同输入重复持久化是幂等操作；
- `minutes/` 按分钟领域规则合并，不以整文件覆盖丢失另一 writer 的分钟；
- `breadth_bootstrap/` 保存按市场、交易日和上午/下午分段的 FAST 引导状态；只有更新 revision 的 writer 才能覆盖同路径状态；
- 交换中断留下的备份会在下次写入前恢复或清理。

manifest 永远最后写入，因此 latest 指针不会先于目标归档出现。

## 完整分钟历史

detail stock 的日级记录位于：

```text
history/minutes/<YYYY-MM-DD>/<code>.json
```

记录包含官方日历预期分钟、完整已终结分钟点、observation 列表、来源异常、冲突、覆盖率、累计量连续性、终态和 replay 资格。形成中的分钟不会进入记录；多次采集和迟到 writer 取并集。已存在分钟若出现不同内容会留下冲突证据并取消 replay 资格，禁止静默改写。

只有同时满足以下条件时 `replay.eligible=true`：

- 日历已验证且会话已完整结束；
- 预期分钟无缺口；
- 时间唯一，累计成交量/额单调；
- 没有分钟版本冲突；
- 至少存在一个可验证的 GitHub Actions observation 身份。

## Reader 与 snapshot locator

`minute_history.load_day(code, session_date, ...)` 读取某股票某交易日的规范记录；`require_replay_eligible=True` 会拒绝不满足回放条件的数据。

每个 detail stock 的 snapshot 包含 `minute_history` locator，记录规范路径、record id、该次 observation 和当时已观察点数。`minute_history.load_from_snapshot(...)` 会验证：

- 路径不能逃逸 history root；
- record id 必须一致；
- 当前规范记录不能少于 snapshot 当时的点数；
- 规范记录必须仍包含该 snapshot 的 observation。

因此 locator 可以安全指向后续只增不减的同一日规范记录，而不依赖模糊的“latest”文件。

## FAST breadth 引导状态

会话宽度状态位于：

```text
history/breadth_bootstrap/CN_A/<YYYY-MM-DD>/<MORNING|AFTERNOON>.json
```

状态以 `CN_A:<session_date>:<segment>` 绑定，记录 owner、租约、尝试次数、bootstrap revision、成功时间和原始 breadth。主工作流及 reusable workflow 先用 Actions concurrency 串行化共享历史的 run，同一进程再用文件锁仲裁 owner；网络抓取在锁外执行。历史 artifact/cache 保存该状态，因此同一分段后续 FAST 不需要重复抓全市场。

`READY` 是持久化成功事实；读取时若 `cache_age_seconds` 超过 600 秒，snapshot 视图输出 `STALE / PARTIAL`，但不会改写成功事实或在同一分段自动重抓。输出同时保留 `source_session`、`fetched_at`、`age_seconds`、`freshness_status` 与 `bootstrap_revision`。失败状态在退避后可重新认领，过期 `PENDING` 租约允许崩溃恢复。下一分段或下一交易日使用不同路径，旧状态不能跨 key 复用。
