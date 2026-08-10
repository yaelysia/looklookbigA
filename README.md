# looklookbigA

一个面向 A 股盘中分析的实时行情与结构化快照工程。项目最初是简单的行情查询站，现在已经扩展为可以被 GitHub Actions / ChatGPT / 其他自动化流程复用的行情采集与分析底座。

当前默认重点跟踪巨人网络（002558）和国电电力（600795），并配置了一组游戏板块对照股；观察列表本身是可配置的，并不限制只能查询这些股票。

## 版本与分支

- **`v1`**：对外复用的稳定分支。推荐其他仓库、长期自动化任务固定使用 `@v1`。
- **`master`**：持续开发分支，新功能先在这里完成 PR 和完整 CI 验证。
- **`market-data`**：主仓库自动生成的日 K、公司事件缓存和轻量盘中历史，不属于发布代码。

`v1` 不会自动跟随 `master`。只有当 `master` 的主行情工作流、reusable selftest 和安全检查都通过后，才通过 `master → v1` 的发布 PR 晋升。详细规则见 `docs/STABLE_V1.md`。

## 当前能力

- **重点标的实时行情**：现价、开高低、均价、涨跌幅、成交额和时间戳。
- **盘中低延迟模式**：交易窗口内 `AUTO` 自动进入 `INTRADAY_FAST`，并发获取重点股 quote/minute、板块 peer、6 个主要指数和官方事件；完整说明与实测见 `docs/INTRADAY_FAST.md`。
- **行情源容错**：FULL 路径重点股和主要指数使用东方财富主源 + 腾讯备用源；主源失败或不可用时自动降级，并记录 provider 状态及双源价格一致性。FAST 的指数使用一次腾讯 Trust-B 批量请求并明确标记 `SINGLE_SOURCE_FAST_PATH`，不伪装为双源共识。
- **当日分钟线**：腾讯分钟行情，生成 5 / 15 / 30 分钟动量、VWAP 位置、日内位置和量能强度。
- **板块对照组**：支持轻量批量行情与组内均值、中位数、涨跌家数、领涨领跌、目标股相对强弱。
- **市场环境与个股归因**：输出上证 / 沪深300 / 中证1000 / 深成 / 创业板 / 科创50、全市场广度、风格差、配置板块环境，以及重点股相对市场 / 板块的结构化 `driver_attribution`。FAST 的全市场 breadth 仅允许 same-session、≤600 秒缓存，否则显式降级；FULL 继续现场完整获取。
- **20～60 日日 K 背景**：MA5 / MA10 / MA20 / MA60、ATR14、5 / 10 / 20 日高低、20 日收益、日线 swing high / low。
- **支撑 / 压力上下文**：综合均线、昨日 OHLC、近期高低和日线拐点生成可解释的候选价位及共振强度。
- **公告 / 公司事件**：从巨潮资讯官方披露源获取 detail stock 最近 7 / 30 / 90 日公告，输出稳定 `event_id`、事件类型、发布时间、官方 PDF、重要性、结构化事实、事件关联、缓存和 provider health；重要公告可从官方 PDF 原文做确定性事实抽取。已经成功得到的官方 PDF facts 在后续较弱 FAST refresh 或失败的 FULL re-enrichment 中不会被擦除。
- **快照增量变化**：自动比较上一份完整归档快照，输出价格 / 成交额 / 分时 / 相对强弱 / 板块排名 / 市场环境 / 新增或更新事件的 `before / after / delta` 和显著性原因。master Realtime 使用上一条成功 run 的 exact `market-history-state` artifact 作为优先 baseline，避免异步 branch persistence 导致跳过上一轮。
- **统一 provenance / freshness / quality / trust / SLA**：重要原始数据和派生数据统一携带来源、时间、freshness、quality、Source Trust 和 Freshness SLA；顶层提供适合 LLM 消费的质量摘要。
- **历史缓存**：主仓库使用独立 `market-data` 分支持久保存日 K、公司事件缓存和轻量盘中快照；同一交易阶段重复分析时日 K 可以做到 0 次网络请求。后台 writer 使用 monotonic revision guard，旧 run 不允许覆盖新状态。
- **实时价格保险**：历史缓存、历史快照、公司事件缓存和日 K 数据被禁止进入 `quote.latest`。盘中实时 quote 失效时，只允许降级到仍然 LIVE 的当日分钟价；两者都不新鲜时当前价直接标记不可用。
- **Reusable workflow 安全边界**：版本绑定到精确 workflow SHA、调用者配置做路径/大小/数量校验、第三方 Actions 固定 commit SHA、行情传输禁止 HTTP 降级、事件 PDF 解析依赖固定版本并固定 SHA256 hash。
- **公共 Web 接口基础滥用防护**：`/` 和 `/quote` 通过 Cloudflare Rate Limiting binding 做服务端限流；限流 binding 不可用时 fail closed，另有 2 秒 isolate-local 短缓存仅用于合并重复上游行情请求。
- **结构化产物**：每次运行生成 `snapshot.json`，重点面向 LLM 直接读取，尽量同时保留结论、原始依据、来源、时间、质量和降级状态。

## 数据源与实时性

目前主要使用：

- 东方财富：FULL 重点标的最新 quote、指数等实时行情主源，以及全市场列表 / breadth 数据；
- 腾讯：重点股 / 指数备用 quote、FAST 批量指数、分钟线、批量轻量行情、前复权日 K；
- 东方财富日 K：腾讯日 K 不可用时的备用源；
- 巨潮资讯（CNINFO）：detail stock 官方公司公告及官方 PDF 文档；
- `market-data` / GitHub Actions cache/artifact：仅用于历史日 K、事件缓存与历史分析上下文，不作为实时现价来源。

行情传输只允许 HTTPS。盘中数据会携带 `market_time_cst`、`lag_seconds` 和 `freshness`。重点标的还会生成 `current_price_guard`，明确记录本次当前价来自实时 quote、实时分钟线，还是已经不可用。

更完整的数据可信度与时间合同见：

- `docs/DATA_PROVENANCE.md`
- `docs/SOURCE_TRUST_MODEL.md`
- `docs/FRESHNESS_SLA.md`
- `docs/INTRADAY_FAST.md`

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

`detail_codes` 会抓实时 quote、分钟线、日 K、官方公司事件和完整分析上下文；`light_codes` / group member 主要用于批量板块对照。

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

`AUTO` 在 A 股交易窗口自动使用 FAST，其他时间使用 FULL。运行后生成 `realtime-snapshot / snapshot.json` 和 `market-history-state` artifact。

master 每轮开始会优先恢复上一条成功 Realtime run 的 exact history artifact，因此上一轮是否已经异步 push 到 `market-data` 不会影响 `changes_since_previous` 的 baseline 连续性；随后独立 `Persist Market History` workflow 再以 monotonic 规则后台提交 branch。

`performance.decision_snapshot_ready_ms` 是 Python engine 从 runner 启动到决策数据完成的耗时；master 在 runner 启动前执行的 exact-baseline API 查询 / artifact restore，以及 checkout / artifact upload 等属于 GitHub Actions 端到端开销，不包含在该指标内。

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
- Safety tests 覆盖 live-price、source resilience、配置边界、event coverage、PDF facts、Source Trust/Freshness SLA、event fact continuity 与 history read-after-write/monotonic persistence。

稳定分支与发布流程见 `docs/STABLE_V1.md`。
