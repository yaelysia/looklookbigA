# looklookbigA

一个面向 A 股盘中分析的实时行情与结构化快照工程。项目最初是简单的行情查询站，现在已经扩展为可以被 GitHub Actions / ChatGPT / 其他自动化流程复用的行情采集与分析底座。

当前默认重点跟踪巨人网络（002558）和国电电力（600795），并配置了一组游戏板块对照股；观察列表本身是可配置的，并不限制只能查询这些股票。

## 版本与分支

- **`v1`**：对外复用的稳定分支。推荐其他仓库、长期自动化任务固定使用 `@v1`。
- **`master`**：持续开发分支，新功能先在这里完成 PR 和完整 CI 验证。
- **`market-data`**：主仓库自动生成的日 K、公司事件、低频资金 / 财务缓存和轻量盘中历史，不属于发布代码。

`v1` 不会自动跟随 `master`。只有当 `master` 的主行情工作流、reusable selftest 和安全检查都通过后，才通过 `master → v1` 的发布 PR 晋升。详细规则见 `docs/STABLE_V1.md`。

## 当前能力

- **重点标的实时行情**：现价、开高低、均价、涨跌幅、成交额和时间戳。
- **盘中低延迟模式**：交易窗口内 `AUTO` 自动进入 `INTRADAY_FAST`，并发获取重点股 quote/minute、板块 peer、6 个主要指数和官方事件；完整说明与实测见 `docs/INTRADAY_FAST.md`。
- **行情源容错**：FULL 路径重点股和主要指数使用东方财富主源 + 腾讯备用源；主源失败或不可用时自动降级，并记录 provider 状态及双源价格一致性。FAST 的指数使用一次腾讯 Trust-B 批量请求并明确标记 `SINGLE_SOURCE_FAST_PATH`，不伪装为双源共识。
- **统一交易日历**：交易日、会话状态、日 K 已完成日期与预期分钟统一由带官方来源和有效期的日历判断；超出覆盖范围时 fail closed 为 `UNVERIFIED`。
- **完整日级分钟历史**：腾讯分钟行情除生成 5 / 15 / 30 分钟动量、VWAP 位置、日内位置和量能强度外，还按交易日/股票合并保存全部已终结分钟，输出缺口、连续性、终态和 replay 资格。
- **资金 / 成交行为上下文**：为 detail stock 输出 `capital_flow`，区分 `OBSERVED / DERIVED / OFFICIAL_DELAYED / VENDOR_ESTIMATE`；包含 1/5/15/30 分钟成交速度、上涨/下跌分钟成交结构、量价确认、方向性 pressure、承接/absorption、VWAP 行为、融资融券延迟数据和同行资金同步性。FAST 只复用现有分钟/quote/上一 exact snapshot 与融资缓存，不增加盘中资金网络依赖；语义与公式见 `docs/CAPITAL_FLOW.md`。
- **基本面 / 财务趋势上下文**：为 detail stock 输出 `fundamentals`，保留公司披露累计口径，同时仅通过可验证的 `Q1 / H1-Q1 / Q3-H1 / FY-Q3` 恢复单季度，连续四个已验证单季度才计算 TTM；提供营收/利润/扣非利润、利润率、ROE、经营现金流质量、资产负债同比、趋势 evidence、财务背离和 `changes_since_previous`。FAST 只读财务缓存，FULL 才并发刷新 main/income/balance/cashflow 四张低频报表；详见 `docs/FUNDAMENTALS.md`。
- **股权 / 股本 / 股东结构上下文**：为 detail stock 输出 `ownership_and_capital`，包含股本历史、实控人与控股股东、前十大及流通股东集中度、机构持仓、股东户数、回购/增减持计划、解禁、质押与资本工具、结构信号，并与 `upcoming_events`、市值口径、exact-history `changes_since_previous` 和统一数据质量合同衔接。所有控制关系、比例和潜在稀释都按证据 fail closed，FAST 不增加低频网络请求；详见 `docs/OWNERSHIP_AND_CAPITAL.md`。
- **板块对照组与同步相对强弱**：除轻量行情、组内均值/中位数和领涨领跌外，还以同一交易日、同一已终结分钟截止点计算重点股相对配置同业篮子、创业板指和中证1000的 5 / 15 / 30 分钟超额收益；分钟缺口和同业覆盖变化会显式降级，详见 `docs/RELATIVE_STRENGTH_WINDOWS.md`。
- **市场环境与个股归因**：输出上证 / 沪深300 / 中证1000 / 深成 / 创业板 / 科创50、全市场广度、风格差、配置板块环境，以及重点股相对市场 / 板块的结构化 `driver_attribution`。FAST 按交易日和上午/下午分段只允许一个并发 owner 引导 breadth，后续刷新读取会话绑定缓存；失败、等待和陈旧状态均显式降级。FULL 继续现场完整获取。
- **20～60 日日 K 背景**：MA5 / MA10 / MA20 / MA60、ATR14、5 / 10 / 20 日高低、20 日收益、日线 swing high / low。
- **支撑 / 压力上下文**：综合均线、昨日 OHLC、近期高低和日线拐点生成可解释的候选价位及共振强度。
- **公告 / 公司事件**：从巨潮资讯官方披露源获取 detail stock 最近 7 / 30 / 90 日公告，输出稳定 `event_id`、事件类型、发布时间、官方 PDF、重要性、结构化事实、事件关联、缓存和 provider health；重要公告可从官方 PDF 原文做确定性事实抽取。已经成功得到的官方 PDF facts 在后续较弱 FAST refresh 或失败的 FULL re-enrichment 中不会被擦除。
- **未来事件日历**：为 detail stock 输出 `upcoming_events`，只复用当前快照中能够证明日期语义的官方公司事件、解禁与回购/增减持计划事实；提供非重叠 7 / 30 / 90 天窗口、最近事件、交易日邻近、同日事件重叠、跨来源去重以及统一 provenance / freshness / quality。模糊 `effective_date` 或无法证明的未来日期会 fail closed，不从历史财报发布时间猜测下一次披露日期；详见 `docs/UPCOMING_EVENTS.md`。
- **快照增量变化**：自动比较上一份完整归档快照，输出价格 / 成交额 / 分时 / 资金行为 / 基本面 / 股权资本结构 / 未来事件 / 相对强弱 / 板块排名 / 市场环境 / 新增或更新事件的 `before / after / delta` 和显著性原因。未来事件只报告新增、移出 upcoming、明确完成/取消、日期/状态变化以及进入 30 日 / 7 日窗口；普通逐日倒计时不制造变化噪声。master Realtime 优先使用同一 run 的前一次成功 attempt，否则使用上一条成功 run，并严格校验 exact `market-history-state` 的 run / attempt / head SHA。
- **统一 provenance / freshness / quality / trust / SLA**：重要原始数据和派生数据统一携带来源、时间、freshness、quality、Source Trust 和 Freshness SLA；顶层提供适合 LLM 消费的质量摘要。
- **历史缓存**：主仓库使用独立 `market-data` 分支持久保存日 K、完整分钟记录、公司事件、融资融券低频缓存、财务报表低频缓存和盘中快照。后台 writer 采用加锁、暂存校验和领域合并；迟到 run 可补齐归档/分钟，但不能回退 latest manifest。
- **实时价格保险**：历史缓存、历史快照、公司事件缓存、资金缓存、财务缓存和日 K 数据被禁止进入 `quote.latest`。盘中实时 quote 失效时，只允许降级到仍然 LIVE 的当日分钟价；两者都不新鲜时当前价直接标记不可用。
- **Reusable workflow 安全边界**：版本绑定到精确 workflow SHA、调用者配置做路径/大小/数量校验、第三方 Actions 固定 commit SHA、行情传输禁止 HTTP 降级、事件 PDF 解析依赖固定版本并固定 SHA256 hash。
- **公共 Web 接口基础滥用防护**：`/` 和 `/quote` 通过 Cloudflare Rate Limiting binding 做服务端限流；限流 binding 不可用时 fail closed，另有 2 秒 isolate-local 短缓存仅用于合并重复上游行情请求。
- **结构化产物**：每次运行生成 `snapshot.json`，重点面向 LLM 直接读取，尽量同时保留结论、原始依据、来源、时间、质量和降级状态。

## 数据源与实时性

目前主要使用：

- 东方财富：FULL 重点标的最新 quote、指数等实时行情主源、全市场列表 / breadth、融资融券披露数据，以及公司财务报表的市场数据供应商入口；融资数据在 schema 中属于 `OFFICIAL_DELAYED`，财务报表属于 `FUNDAMENTALS`，但通过东方财富取得时 Source Trust 仍为 B，不伪装成交易所 / 公司第一方来源；
- 腾讯：重点股 / 指数备用 quote、FAST 批量指数、分钟线、批量轻量行情、前复权日 K；
- 东方财富日 K：腾讯日 K 不可用时的备用源；
- 巨潮资讯（CNINFO）：detail stock 官方公司公告及官方 PDF 文档；
- `market-data` / GitHub Actions cache/artifact：仅用于历史日 K、事件 / 资金 / 财务低频缓存与历史分析上下文，不作为实时现价来源。

行情传输只允许 HTTPS。盘中数据会携带 `market_time_cst`、`lag_seconds` 和 `freshness`。重点标的还会生成 `current_price_guard`，明确记录本次当前价来自实时 quote、实时分钟线，还是已经不可用。

更完整的数据可信度与时间合同见：

- `docs/DATA_PROVENANCE.md`
- `docs/SOURCE_TRUST_MODEL.md`
- `docs/FRESHNESS_SLA.md`
- `docs/INTRADAY_FAST.md`
- `docs/CAPITAL_FLOW.md`
- `docs/FUNDAMENTALS.md`
- `docs/OWNERSHIP_AND_CAPITAL.md`
- `docs/UPCOMING_EVENTS.md`
- `docs/MARKET_CALENDAR.md`
- `docs/MARKET_HISTORY.md`
- `docs/RELATIVE_STRENGTH_WINDOWS.md`

## 默认观察列表

配置位于：

```text
config/quote_watchlist.json
```

基本结构：

```json
{
  "detail_codes": ["002558", "600795"],
  "light_codes": [],
  "groups": {
    "game_sector": {
      "label": "A股游戏板块对照组",
      "target_code": "002558",
      "member_codes": ["002602", "002555", "002517"]
    }
  },
  "max_total_codes": 50,
  "event_lookback_days": 30
}
```

`detail_codes` 会抓实时 quote、分钟线、日 K、官方公司事件，并生成资金/成交行为、基本面、未来事件日历与完整分析上下文；`light_codes` / group member 主要用于批量板块对照。

`event_lookback_days` 允许 `7 / 30 / 90`，默认 `30`。它只控制 detail stock 的公告查询窗口，不影响行情实时性。

对外 reusable workflow 会把该配置视为不可信输入：配置最大 32 KiB，`max_total_codes` 硬上限为 50，并统一统计 detail/light/group 合并后的唯一股票数量；重复 code、非法路径、目录逃逸、symlink 逃逸以及非法公告回看窗口会被直接拒绝。

## 在本仓库运行

GitHub Actions 工作流：

```text
.github/workflows/realtime-quotes.yml
```

支持：

```text
AUTO
INTRADAY_FAST
FULL
```

`AUTO` 在 A 股交易窗口自动使用 FAST，其他时间使用 FULL。FAST 对融资融券和基本面都只读低频缓存，不增加这两类网络依赖；FULL 负责刷新。运行后生成 `realtime-snapshot / snapshot.json` 和 `market-history-state` artifact。

master 每轮开始会优先恢复同一 run 的前一次成功 attempt；若不存在，才恢复上一条独立成功 Realtime run。history artifact 必须与目标 run / attempt / head SHA 完全一致，因此异步 push 到 `market-data` 的时序不会破坏 `changes_since_previous` 的 baseline 连续性；随后独立 `Persist Market History` workflow 再以单调合并规则后台提交 branch。

`performance.decision_snapshot_ready_ms` 是 Python engine 从 runner 启动到决策数据完成的耗时；master 在 runner 启动前执行的 exact-baseline API 查询 / artifact restore，以及 checkout / artifact upload 等属于 GitHub Actions 端到端开销，不包含在该指标内。

## LLM / Agent 立即刷新实时行情

LLM / Agent 需要“现在重新抓一次”时，**首选新的 `workflow_dispatch`**。新的 dispatch 会创建新的 workflow run，并允许显式选择 `AUTO / INTRADAY_FAST / FULL`；这是语义最清晰的刷新方式。

如果 Agent 工具没有 `workflow_dispatch`，但提供 GitHub Actions 的单 job rerun，可以把最近一次可信 `master` run 的 `fetch-quotes` job 作为兼容 fallback。这个 fallback 会重新执行实时采集和 artifact 上传，但它**不是一条新的 dispatch**，必须保留更严格的 run / attempt / artifact 身份校验。

为保证同一个 workflow run 的后续 attempt 能重复发布同名产物，`fetch-quotes` 对 `realtime-snapshot` 和 `market-history-state` 的 `actions/upload-artifact` 均显式设置 `overwrite: true`。rerun 会替换该 run 内的旧同名 artifact，并产生新的 artifact identity；因此下面仍以“新 artifact ID 不在 rerun 前基线集合中 + `created_at` 不早于 rerun 开始时间”作为 fail-closed 识别条件。若没有观察到新的 artifact identity，则不能把旧 artifact 当成本次刷新结果。

### 兼容 fallback 的选择条件

执行 rerun 前先读取当前 `master` SHA，记为 `M`，然后只接受同时满足以下条件的历史 run：

- workflow 为 `.github/workflows/realtime-quotes.yml` / `Realtime A-share Quotes`；
- `head_branch == master`；
- `head_sha == M`，禁止为了“能 rerun”退回更旧 commit；
- 原 run 已 `completed + success`，并且其中 `fetch-quotes` job 成功；
- 用于通用“立即刷新”时，不复用带旧 Alpha operation identity 的 run。当前 workflow 对 identity-bound dispatch 使用 `alpha-refresh corr=...` run name；除非调用方明确是在恢复同一 operation identity，否则应优先选择普通 `push` run 或其他没有该 identity 的可信 run。

如果当前 `master` 没有满足条件的成功 run，则这个兼容路径应 fail closed：改用 `workflow_dispatch`，或先等待/产生当前 `master` 的可信成功 run；不要 rerun 旧 SHA 后把结果称为当前版本数据。

### `rerun_workflow_job` 操作顺序

1. 记录候选 run 的 `run_id`、当前 `run_attempt=A`、`head_sha=M`，以及 rerun 前该 run 下所有 artifact ID。
2. 从该 run 的 jobs 中精确选择名称为 `fetch-quotes` 的成功 job，并调用 `rerun_workflow_job(job_id)`。
3. 轮询**同一个 `run_id`**，直到 `run_attempt > A` 且最新 attempt `completed + success`；同时再次读取最新 attempt 的 jobs，确认 `fetch-quotes` 本身成功。
4. 重新列出该 run 的 artifacts。只接受名称为 `realtime-snapshot`、`workflow_run.id == run_id`、`workflow_run.head_sha == M`，且 **artifact ID 不在 rerun 前基线集合中** 的新 artifact。
5. 新 artifact 还应满足 `created_at` 不早于本次 rerun 的开始时间。若出现多个新候选，按 `created_at`、再按 artifact ID 取最新；如果无法唯一证明 artifact 来自本次 attempt，则不要读取旧 artifact 冒充本次刷新结果。
6. 下载该 artifact 中的 `snapshot.json` 后，再进入行情分析。

`realtime-snapshot` 当前由 `fetch-quotes` job 上传，另一个 `market-history-state` artifact 只用于历史连续性；不要把 `market-history-state` 或 `market-data` 当作实时现价来源。

### rerun 与新 dispatch 的差异

`rerun_workflow_job` 复用原 run 的 `run_id`、`head_sha`、workflow 定义以及原 event/input 上下文。它会产生新的 attempt，但不能像新 dispatch 一样切换到更新后的 commit 或重新指定新的 workflow inputs。

因此：

- 原 run 是普通 `push` 时，`mode` 没有 dispatch input，当前 workflow 会按 `AUTO` 解析；
- 如果需要明确指定 `FULL` 或 `INTRADAY_FAST`，应使用新的 `workflow_dispatch`；
- 如果 `master` 在原 run 后已经前进，rerun 仍会执行旧 `head_sha`，此时不能作为当前版本刷新；
- identity-bound 的旧 dispatch 不应被当成通用 refresh run 重用，以免把旧 correlation / command identity 带入新的业务语义。

### 实时价格安全边界

无论是新 dispatch 还是 job rerun，**artifact 生成成功都不等于“当前价一定可用”**。读取 `snapshot.json` 后仍必须校验：

- detail stock 的 `current_price_guard`（调用层如另有 `live_price_guard`，也必须同时通过）；
- quote / minute 的 `freshness`、`market_time_cst`、`lag_seconds`；
- source / provider quality 与降级状态；
- 当前价是否来自本次仍然 LIVE 的 quote，或允许的当日 LIVE minute fallback。

历史 snapshot、`market-data`、日 K、公司事件、资金/财务/股权缓存都不能进入当前价判定。若实时 quote 与允许的当日分钟价都不满足 freshness，必须把当前价视为 unavailable，而不是回退到旧 artifact。

### 可直接给 Agent 的操作指令

```text
Read current master SHA M.
Find the newest successful Realtime A-share Quotes run on master
whose head_sha is exactly M and which is not an unrelated identity-bound alpha-refresh run.
Find its successful fetch-quotes job.
Record run_id, baseline run_attempt, rerun start time, and all existing artifact IDs.
Call rerun_workflow_job(fetch-quotes job_id).
Poll the same run until run_attempt increases and the latest attempt succeeds.
Verify fetch-quotes succeeded in that attempt.
List artifacts again and select only a newly created realtime-snapshot artifact
for the same run_id/head_sha whose ID was not present before the rerun.
If the new artifact cannot be identified unambiguously, fail closed.
Download snapshot.json.
Validate current_price_guard / live-price freshness / source quality before using any current price.
Never use historical snapshots, market-data, or caches as the current price.
```

## 在其他仓库复用

项目提供 reusable workflow：

```text
.github/workflows/reusable-a-share-quotes.yml
```

### 推荐：固定使用稳定版 v1

```yaml
name: A-share snapshot

on:
  workflow_dispatch:

jobs:
  quotes:
    uses: yaelysia/looklookbigA/.github/workflows/reusable-a-share-quotes.yml@v1
    with:
      enable_history_cache: true
      execution_mode: AUTO
```

运行结果会产生 `realtime-snapshot` artifact，其中包含 `snapshot.json`。

reusable workflow 未显式覆盖 `source_ref` 时，会使用 `job.workflow_sha` 把实际执行的 engine 绑定到定义当前 reusable workflow 的精确 commit，并在 checkout 后再次校验 SHA。`source_ref` 仅作为高级 override，并且只接受完整 40 位 commit SHA。

需要验证开发版时可使用 `@master`；需要最高不可变性时可直接把 workflow `uses:` 固定到完整 commit SHA。

## 安全与发布

- 所有第三方 GitHub Actions 固定到完整 commit SHA；
- Tencent 行情不允许 HTTP fallback；
- CNINFO PDF 初始 URL、每次 redirect 和最终 URL 均限制在官方 HTTPS host；
- required `pre-merge-security-gate` 在 `master` / `v1` 上无 paths 条件，任何 PR 都必须通过；
- required reusable smoke 强制 FULL，push selftest 额外覆盖 FAST；
- Safety tests 覆盖 live-price、source resilience、配置边界、资金行为与融资 provider 口径、财务累计→单季度归一化、TTM 连续性、财务趋势 evidence、partial-report cache continuity、upcoming-events date/dedupe/trading-day/changes/quality contract、event coverage、PDF facts、Source Trust/Freshness SLA、交易日历、exact-attempt artifact、history 单调合并与完整分钟 replay。

稳定分支与发布流程见 `docs/STABLE_V1.md`。
