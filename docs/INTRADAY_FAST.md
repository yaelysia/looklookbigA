# INTRADAY_FAST：盘中低延迟执行模式

`INTRADAY_FAST` 用于盘中需要快速决策的场景。它不是删减版行情抓取器，而是把“必须等待的实时信息”和“可以复用或延后的慢信息”分开，使非关键数据源失败时不会拖住实时 quote / minute / peer / index。

## 模式选择

主工作流和 reusable workflow 都支持：

```text
AUTO
INTRADAY_FAST / FAST
FULL
```

`AUTO` 使用项目现有的 A 股交易窗口判断：交易窗口内自动选择 `INTRADAY_FAST`，盘后自动选择 `FULL`。`workflow_dispatch` 可以显式覆盖；reusable workflow 使用 `execution_mode` 输入。

## FAST 的关键路径

FAST 必须等待：

```text
重点股 realtime quote
重点股 minute series
板块 peer quotes
6 个主要指数
官方公司事件 discovery
本地 intraday / changes / metadata 计算
```

它们仍然接受现有 `live_price_guard`、Source Trust、Freshness SLA、quality/provenance 约束。FAST 只改变调度与等待策略，不允许旧数据冒充实时数据。

### Detail stock 并发

- 多只 detail stock 并发采集；
- 单只 detail stock 内 quote 与 minute 并发；
- detail / light peer / indices 三组并发。

### 主要指数

`FULL` 保持 Eastmoney + Tencent 双源一致性检查。

`INTRADAY_FAST` 使用一次 Tencent 批量请求获取 6 个主要指数，避免等待 6 个 Eastmoney 单请求产生长尾。Tencent 仍是 Source Trust B；FAST snapshot 会显式记录：

```text
selected_source = Tencent
fallback_used = true
selection_reason = INTRADAY_FAST_TENCENT_BATCH
consensus.status = SINGLE_SOURCE_FAST_PATH
Eastmoney.status = SKIPPED_FAST_PATH
```

因此 FAST 不会把“单源快速指数”描述成双源共识。

### Daily-K

盘中已完成的日 K 本身不会分钟级变化，因此 FAST 优先复用历史缓存：

- 当日已经成功验证过的 cache：`HIT`，0 次 daily-K 网络请求；
- 旧 cache、失败 cache 或无法证明当日已验证：允许作为历史上下文，但状态为 `FAST_REUSE_UNVERIFIED`；metadata 会 `DEGRADED`，session freshness SLA 保持 `UNMEASURED`；
- 冷启动没有足够历史 bars 时才进行网络 bootstrap。

这延续了项目的实时价格边界：daily-K/cache 永远不能进入 `quote.latest`。

### Market breadth

全市场 breadth 很重要，但不是盘中当前价的 critical dependency。真实 Actions 测试证明，即使设置短 socket timeout，上游连接、多页查询或失败路径仍可能制造 >10 秒长尾。

因此 FAST 的规则是按交易会话分段做受控引导：

```text
交易日 acquisition window 内首次 FAST
        ↓
按 CN_A:<session_date>:MORNING|AFTERNOON 加锁认领唯一 owner
        ↓
owner 获取一次完整 breadth；同段后续刷新只读历史状态缓存
        ↓
PENDING / FAILED / STALE / UNVERIFIED 显式降级，不伪装成新鲜数据
```

上午从 09:25 acquisition window 开始，下午从 12:55 acquisition window 开始，两个分段分别最多成功引导一次。失败在 60 秒退避后允许重试；owner 的 300 秒租约过期后允许崩溃恢复。成功缓存超过 600 秒只会返回 `STALE`，不会在同一分段偷偷再次抓取；下一分段或下一交易日使用新 key。主工作流按 ref、reusable workflow 按 caller repository 与 cache namespace 使用 `cancel-in-progress=false` barrier 串行化共享历史状态的 Actions run，进程内文件锁再防止同一 run 的重复 owner。

状态记录持久化到 `market-history-state`。breadth / market environment 明确输出 `session_date`、`session_segment`、`bootstrap_state`、`bootstrap_at`、`cache_age_seconds`，并以 `source_session`、`fetched_at`、`age_seconds`、`freshness_status`、`bootstrap_revision` 保留缓存来源与新鲜度证据。FAST 不跨 session 复用昨天的广度。

`FULL` 仍执行完整 full-universe + 严格 fallback breadth 采集。

### 公司公告 / 事件

公告 discovery 不应因为追求速度而完全跳过。FAST 仍刷新 CNINFO 官方事件，但：

- detail stocks 的事件查询并发；
- FAST 请求使用较短网络预算；
- 事件预取与行情采集同时开始，两者耗时重叠；
- CNINFO PDF facts extraction 在 FAST 中 `DEFERRED`，FULL 再执行。

因此盘中仍可以看到刚出现的官方事件，而 PDF 原文深层事实抽取不会阻塞实时决策。

#### 官方 PDF facts 的单调性

`DEFERRED` 只表示“本轮不重新解析 PDF”，不能意味着删除历史上已经成功获得的事实。对于相同 `event_id` 且能确认 `source_document_id`（或官方 document URL）未变化的 CNINFO immutable document：

```text
FULL 曾成功得到 ORIGINAL_PDF_TEXT facts
        ↓
FAST overlap refresh 只返回 TITLE/API facts
        ↓
保留已有 ORIGINAL_PDF_TEXT + document_extraction=OK
        ↓
不产生假的 changes_since_previous.events.updated
```

新的、不同 document 不继承旧 facts。FULL 后续若成功重新解析 PDF，可以替换 document fact layer；如果本次 FULL PDF 下载/解析失败，则本轮 `fact_enrichment` health 仍报告失败/部分降级，但上一次已经成功验证的 PDF facts 不会被擦成 `UNAVAILABLE`。

## Workflow 启动成本与历史连续性

FAST 不安装 `pypdf`。`requirements-event-facts.txt` 的 pinned/hash-verified parser 只在 `FULL` 安装。

主 realtime workflow 不再同步执行 `persist-history` 第二个 job。实时 workflow 上传 `snapshot.json` / `market-history-state` 后即可结束；master 的历史持久化由独立 `Persist Market History` workflow 通过 `workflow_run` 在后台处理。

后台持久化只监听 `master` 上成功完成的 `Realtime A-share Quotes`，不会处理 feature/PR 分支产物。

### Read-after-write baseline barrier

后台写入不能破坏 `changes_since_previous` 的连续性。master 上每次新的 Realtime run 开始时，会先解析“上一条成功的 Realtime A-share Quotes run”，优先直接下载那个 exact run 的 immutable `market-history-state` artifact，并用它作为本轮 `.market-data/history`。

因此即使出现：

```text
Run A realtime = success
Persist A 尚未 push market-data
Run B 立即启动
```

Run B 仍然直接读取 Run A 的 exact artifact，`previous_snapshot_path` 指向 A，而不会退回 A-1。只有 exact artifact 已过期/下载失败时才回退 `market-data`；回退时必须用 run revision 或 legacy timestamp 证明 branch 状态至少不落后于上一成功 run，否则 fail closed。

同一 branch 的 Realtime workflow还使用 `cancel-in-progress=false` 的 concurrency barrier，避免多轮实时采集互相穿插。

### Monotonic background writer

`Persist Market History` 不再把 artifact 直接解压到 `history/`。它先把 incoming state 放入隔离目录，并 checkout 触发该 Realtime run 的 exact `head_sha`，使用对应版本的 `history_continuity.py` 比较：

```text
latest_runner_time_cst
+ latest_run_id
+ latest_run_attempt
```

snapshot 时间是主排序依据，run id / attempt 用于同时间 tie-break。只有 incoming state 严格更新时才允许替换 `market-data/history`；较旧或重复的 persistence 到达时直接 no-op。因此即使异步 writer 到达顺序反转，也不能把 manifest、event cache 或 daily-K cache 回滚。

## Performance telemetry

每份最终 snapshot 会包含：

```json
{
  "performance": {
    "mode": "INTRADAY_FAST",
    "decision_snapshot_ready_ms": 2493.657,
    "target_ms": 10000,
    "hard_limit_ms": 15000,
    "within_target": true,
    "within_hard_limit": true,
    "stages_ms": {}
  }
}
```

`decision_snapshot_ready_ms` 是 Python engine 从 runner 启动到本轮决策数据完成的耗时，记录在本地 history archive 之前。master 的 exact-previous-run API 查询 / artifact restore 发生在 runner 启动前，因此不包含在这个字段中；它属于 GitHub Actions 端到端开销，后续应单独监控。

目标：

```text
FAST engine target       <= 10s
FAST engine hard budget  <= 15s
```

这些是性能 SLO，不会覆盖 freshness SLA。即使运行很快，quote/minute 不满足实时性要求时仍然不能被当成 live current price。

## 2026-08-10 真实 Actions benchmark

优化前，master 的一次真实 09:30 盘中运行：

```text
base collection / snapshot       ~52.9s
完整 runner 到 metadata         ~56.9s
```

主要长尾来自串行 detail、全市场 breadth 失败重试，以及慢数据 enrichment。

第一版 FAST 曾出现：

```text
best run                         ~9.15s
另一轮 breadth/index 长尾         ~16.54s
```

第二轮结果曾促使 breadth 退出每轮刷新路径，并将 FAST indices 改为单次 Tencent batch、events 与 market collection 重叠。现在的新会话状态机只在上午/下午分段首次 FAST 做一次受控 bootstrap，避免重新引入“每轮都抓全市场”的长尾。

最终 continuity hardening 后的 exact-head reusable FAST selftest：

```text
base_collection                  2457 ms
detail_stocks                    2454 ms
indices_and_breadth              1414 ms
company_events_prefetch          2077 ms  # 与 base_collection 重叠
decision_snapshot_ready          2494 ms
```

对应数据仍满足：

```text
detail stock live
peer quotes complete
6/6 indices
company events OK
live_price_guard OK
critical_data_ready = true
daily_k_network_requests = 0
```

因此 engine 决策路径仍稳定在约 2～5 秒；GitHub Actions checkout、master exact-baseline API/artifact 恢复、artifact 上传会额外增加端到端 UI 等待时间，但不会再通过慢行情/事件链把 runner 拖回几十秒。

## FULL 模式没有被弱化

FAST 是额外执行路径，不替代 FULL：

- required `pre-merge-security-gate` 的 reusable merge-ref smoke 强制 `execution_mode: FULL`；
- FULL 继续安装 hash-pinned PDF parser；
- FULL 继续做双源指数共识、完整 breadth、公司事件 PDF facts 和完整 slow context；
- push `Reusable Workflow Selftest` 额外强制 FAST，保证两条路径都持续被真实 Actions 覆盖；
- required Safety tests 额外覆盖 event fact monotonicity 和两轮 history read-after-write/乱序 writer 状态机。

## 后续方向

如果后续需要把“点击触发到 LLM 可读”继续压到稳定 1～3 秒以下，主要瓶颈将不再是 Python pipeline，而是 GitHub Actions 冷启动、checkout、exact-baseline restore 与 artifact 传输。届时更适合把 realtime fast plane 放到 Cloudflare Worker / 常驻轻服务，而 GitHub Actions 保留为 FULL ETL、历史与验证平面。
