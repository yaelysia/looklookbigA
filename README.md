# looklookbigA

一个面向 A 股盘中分析的实时行情与结构化快照工程。项目最初是简单的行情查询站，现在已经扩展为可以被 GitHub Actions / ChatGPT / 其他自动化流程复用的行情采集与分析底座。

当前默认重点跟踪巨人网络（002558）和国电电力（600795），并配置了一组游戏板块对照股；观察列表本身是可配置的，并不限制只能查询这些股票。

## 版本与分支

- **`v1`**：对外复用的稳定分支。推荐其他仓库、长期自动化任务固定使用 `@v1`。
- **`master`**：持续开发分支，新功能先在这里完成 PR 和完整 CI 验证。
- **`market-data`**：主仓库自动生成的日 K 缓存和轻量盘中历史，不属于发布代码。

`v1` 不会自动跟随 `master`。只有当 `master` 的主行情工作流、reusable selftest 和安全检查都通过后，才通过 `master → v1` 的发布 PR 晋升。详细规则见 `docs/STABLE_V1.md`。

## 当前能力

- **重点标的实时行情**：现价、开高低、均价、涨跌幅、成交额和时间戳。
- **行情源容错**：重点股和主要指数使用东方财富主源 + 腾讯备用源；主源失败或不可用时自动降级，并记录每个 provider 的状态及双源价格一致性。
- **当日分钟线**：腾讯分钟行情，生成 5 / 15 / 30 分钟动量、VWAP 位置、日内位置和量能强度。
- **板块对照组**：支持轻量批量行情与组内均值、中位数、涨跌家数、领涨领跌、目标股相对强弱。
- **市场环境与个股归因**：输出上证 / 沪深300 / 中证1000 / 深成 / 创业板 / 科创50、全市场广度、风格差、配置板块环境，以及重点股相对市场 / 板块的结构化 `driver_attribution`。
- **20～60 日日 K 背景**：MA5 / MA10 / MA20 / MA60、ATR14、5 / 10 / 20 日高低、20 日收益、日线 swing high / low。
- **支撑 / 压力上下文**：综合均线、昨日 OHLC、近期高低和日线拐点生成可解释的候选价位及共振强度。
- **历史缓存**：主仓库使用独立 `market-data` 分支持久保存日 K 和轻量盘中快照；同一交易阶段重复分析时日 K 可以做到 0 次网络请求。
- **实时价格保险**：历史缓存、历史快照和日 K 数据被禁止进入 `quote.latest`。盘中实时 quote 失效时，只允许降级到仍然 LIVE 的当日分钟价；两者都不新鲜时当前价直接标记不可用。
- **Reusable workflow 安全边界**：版本绑定到精确 workflow SHA、调用者配置做路径/大小/数量校验、第三方 Actions 固定 commit SHA、行情传输禁止 HTTP 降级。
- **公共 Web 接口基础滥用防护**：`/` 和 `/quote` 通过 Cloudflare Rate Limiting binding 做服务端限流；限流 binding 不可用时 fail closed，另有 2 秒 isolate-local 短缓存仅用于合并重复上游行情请求。
- **结构化产物**：每次运行生成 `snapshot.json`，包含实时行情、分时结构、日 K 背景、板块对照、市场环境、个股驱动归因、历史缓存状态、行情源容错状态和实时价格保护状态。

## 数据源与实时性

目前主要使用：

- 东方财富：重点标的最新 quote、指数等实时行情的主源，以及全市场列表 / breadth 数据；
- 腾讯：重点股 / 指数备用 quote、分钟线、批量轻量行情、前复权日 K；
- 东方财富日 K：腾讯日 K 不可用时的备用源；
- `market-data` / GitHub Actions cache：**仅用于历史日 K 与历史分析上下文，不作为实时现价来源**。

行情传输只允许 HTTPS。腾讯 quote 模块本身只构造 `https://qt.gtimg.cn` 请求；HTTPS 请求失败会继续走其他已验证的 HTTPS source / error 路径，代码中不存在明文 HTTP fallback。

盘中数据会携带 `market_time_cst`、`lag_seconds` 和 `freshness`。重点标的还会生成 `current_price_guard`，用于明确记录本次当前价来自实时 quote、实时分钟线，还是已经不可用。

### 行情源容错规则

重点股票和主要指数的 quote 会同时尝试东方财富与腾讯，优先使用可用的东方财富结果；东方财富失败、缺失或新鲜度不满足要求时，自动使用腾讯。两个来源都可用时会计算价格差异：

```text
Eastmoney + Tencent
        ↓
provider health / freshness
        ↓
price consensus
        ↓
selected quote
```

每个重点 quote 会带有 `quote.resilience`，其中包括：

- `selected_source` / `fallback_used` / `selection_reason`；
- 东方财富、腾讯各自的 `status`、`freshness`、时间戳、延迟和错误；
- `consensus.status` 与双源价格差。

当前一致性分级为：价格差不超过 `0.08%` 视为 `CONSISTENT`，超过 `0.35%` 视为 `DIVERGENT`。如果两个实时源都可用但明显分歧，会优先选择时间更新的一侧并保留告警信息。

轻量板块股票默认一次性走腾讯批量行情，批量接口漏失的股票会回退到双源单股查询。分钟线目前仍保留腾讯单源：在没有验证第二个分钟源与现有累计成交量 / 成交额语义完全一致前，不为了形式上的“双源”引入可能错误的分时数据；这一状态会写入 `quote_resilience.minute_data_policy`。

行情源容错与 `live_price_guard` 是两层不同机制：前者负责“尽量取得正确实时数据”，后者负责“任何 stale / history / cache 数据都不能冒充当前价”。

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
  "max_total_codes": 50
}
```

`detail_codes` 会抓实时 quote、分钟线、日 K 和完整分析上下文；`light_codes` / group member 主要用于批量板块对照。

对外 reusable workflow 会把该配置视为不可信输入：配置最大 32 KiB，`max_total_codes` 硬上限为 50，并统一统计 detail/light/group 合并后的唯一股票数量；单 group、member 数量和原始 code entry 也有上限。重复 code、非法路径、目录逃逸和 symlink 逃逸会被直接拒绝。

## 在本仓库运行

GitHub Actions 工作流：

```text
.github/workflows/realtime-quotes.yml
```

可以通过 `workflow_dispatch` 手动运行。运行后会生成：

```text
realtime-snapshot / snapshot.json
```

主仓库运行成功后还会把日 K 缓存和轻量盘中快照写入独立的 `market-data` 分支，不污染 `master` 的代码提交历史。

## 在其他仓库复用

项目提供 reusable workflow：

```text
.github/workflows/reusable-a-share-quotes.yml
```

### 推荐：固定使用稳定版 v1

调用者自己的仓库只需要增加一个很小的 workflow：

```yaml
name: A-share snapshot

on:
  workflow_dispatch:

jobs:
  quotes:
    uses: yaelysia/looklookbigA/.github/workflows/reusable-a-share-quotes.yml@v1
    with:
      enable_history_cache: true
```

运行结果会产生 `realtime-snapshot` artifact，其中包含 `snapshot.json`。

不再需要额外传 `source_ref: v1`。reusable workflow 未显式覆盖时，会使用 `job.workflow_sha` 把实际执行的 looklookbigA engine 绑定到定义当前 reusable workflow 的精确 commit，并在 checkout 后再次校验 SHA。

### 使用最新开发版

需要验证尚未晋升到稳定版的新能力时，可以显式使用 `master`：

```yaml
jobs:
  quotes:
    uses: yaelysia/looklookbigA/.github/workflows/reusable-a-share-quotes.yml@master
```

`master` 是可变开发入口，不推荐用于需要长期稳定复现的自动化任务。

### 最高不可变性：固定完整 commit SHA

如果调用方希望把 workflow 定义本身也锁定到完全不可变版本，可以直接：

```yaml
jobs:
  quotes:
    uses: yaelysia/looklookbigA/.github/workflows/reusable-a-share-quotes.yml@<40-char-commit-sha>
```

`source_ref` 仅保留为高级 override；如果显式传入，只接受完整 40 位 commit SHA，不接受 `master`、`v1` 或其他可变 branch/tag。

### 使用调用者自己的观察列表

推荐在调用者仓库放置：

```text
config/quote_watchlist.json
```

reusable workflow 默认会优先读取这个文件。如果调用者仓库没有该文件，则使用 looklookbigA 对应版本自带的默认配置。

也可以直接传入 JSON：

```yaml
jobs:
  quotes:
    uses: yaelysia/looklookbigA/.github/workflows/reusable-a-share-quotes.yml@v1
    with:
      config_json: >-
        {"detail_codes":["300750"],"light_codes":["002594"],"groups":{},"max_total_codes":20}
```

`config_path` 必须是 caller repository 内部的相对路径。绝对路径、`../` 逃逸和指向仓库外的 symlink 都会被拒绝。

### reusable workflow 历史缓存

外部仓库默认使用 GitHub Actions cache 保存日 K 历史，因此不要求调用者创建 `market-data` 分支，也不需要 `contents: write` 权限。

```yaml
with:
  enable_history_cache: true
  cache_namespace: default
```

第一次运行会初始化历史，之后同一交易阶段通常可以直接命中缓存。跨交易阶段只校验最近少量 K 线；检测到前复权变化或历史缺口时才会完整刷新。

## GitHub Actions 供应链安全

仓库核心 workflow 中的 `actions/checkout`、`actions/cache`、`actions/upload-artifact`、`actions/download-artifact` 等第三方 Action 均固定到完整 commit SHA，而不是可变 `@v4` tag。

`.github/dependabot.yml` 会继续跟踪 GitHub Actions 上游版本更新；升级时由 Dependabot / 人工 review 更新固定 SHA，再经过现有安全测试验证。

## `snapshot.json` 主要结构

```text
snapshot.json
├─ detail_stocks
│  └─ <code>
│     ├─ quote
│     ├─ minutes
│     ├─ intraday
│     └─ daily_context
├─ light_stocks
├─ groups
├─ indices
├─ market_environment
│  ├─ indices
│  ├─ breadth
│  ├─ regime
│  ├─ style
│  ├─ groups
│  └─ targets.<code>.driver_attribution
├─ history
├─ live_price_guard
└─ quote_resilience
```

其中：

- `quote`：联网获得的当前行情，并包含该 quote 的双源容错 / 一致性元数据；
- `intraday`：分时结构和最终可用当前价来源；
- `daily_context`：历史日 K 指标及支撑压力；
- `market_environment`：面向 LLM 的市场环境、风格、全市场广度、板块环境和个股驱动归因；
- `history`：缓存状态和历史快照位置；
- `live_price_guard`：实时价格源安全检查；
- `quote_resilience`：本次运行的 provider 使用情况、fallback 数量、双源分歧和不可用统计。

历史日 K 的 `source` 可能显示为 `History cache (...)`，这是正常的；**只有 `daily_context` 可以使用历史缓存，`quote.latest` 不允许使用任何 History / cache / snapshot 来源。**

## 面向 LLM 的 `market_environment`

`market_environment` 的设计原则是：**结构化 JSON 是主输出，`summary` 只用于快速浏览。** 上层模型应该优先读取原始 reference、spread、breadth 和 `driver_attribution.evidence`，而不是把一个枚举标签当成不可复核的事实。

主要结构：

```text
market_environment
├─ status / confidence
├─ indices
│  ├─ members[]
│  ├─ broad_market_reference_percent
│  └─ dispersion_percent
├─ breadth
│  ├─ status / estimated / source
│  ├─ overall
│  ├─ boards
│  ├─ exchanges
│  └─ sampling
├─ regime
├─ style
│  ├─ references
│  └─ spreads
├─ groups
├─ targets
│  └─ <code>
│     ├─ relative_strength
│     ├─ driver_attribution
│     │  ├─ primary_driver
│     │  ├─ confidence
│     │  ├─ evidence
│     │  └─ reason_codes
│     └─ intraday_context
├─ data_quality
└─ summary
```

### 宽基 / 风格

当前使用上证指数、沪深300、中证1000、深证成指、创业板指、科创50。`broad_market_reference_percent` 使用上证 + 沪深300 + 深成构造更偏“整体市场”的参考；中证1000、创业板、科创50主要用于小盘 / 成长风格差，不直接混入个股的 broad-market reference。

`style.spreads` 目前包括：

- `small_vs_large_percent`：中证1000 - 沪深300；
- `growth_vs_large_percent`：(创业板 + 科创50)/2 - 沪深300；
- `shenzhen_vs_shanghai_percent`：深成 - 上证。

### 全市场 breadth 的精确值与估算值

全市场数据会先尝试一次完整 universe 获取：

- 成功时：`breadth.status=OK`、`estimated=false`，上涨 / 下跌 / 平盘、成交额、板块/交易所广度以及涨跌停近似统计均可直接使用；
- 完整 universe 不可用时：切换为按股票代码排序、跨全市场均匀抽取页面的确定性系统样本。当前目标约 8 页、最多约 800 只，实际 sample size 和 coverage 会写入 `breadth.sampling`。

样本模式必须按下面的语义读取：

```text
breadth.status = PARTIAL
breadth.estimated = true
breadth.overall.up_count / down_count / flat_count = 估算值
breadth.overall.unavailable_change_count = 无有效涨跌数据的估算数量
breadth.overall.up_ratio_percent / down_ratio_percent = 样本中有有效涨跌数据标的的比例
breadth.overall.amount_1e8 = null
breadth.limit_statistics.available = false
```

样本模式下**不会**把样本成交额外推成全市场总成交额，也不会把少量涨跌停事件线性放大；这两类字段宁可 `null`，也不输出看似精确但统计意义很差的数字。`sample_amount_1e8` 仅表示样本自身成交额。

如果完整 universe 和系统样本都失败，`breadth.status=ERROR`，`regime` 会降级为 `UNKNOWN`；这个失败只影响市场环境层，不会让实时 quote / `live_price_guard` 主链路失败。

### 市场 regime

`regime.status` 用宽基市场 reference + breadth 区分：

- `BROAD_RISK_ON`：宽基上涨且多数个股同步上涨；
- `BROAD_RISK_OFF`：宽基下跌且多数个股同步下跌；
- `INDEX_UP_NARROW`：指数上涨，但个股上涨占比不足；
- `INDEX_DOWN_BREADTH_RESILIENT`：指数偏弱，但市场广度没有同步恶化；
- `ROTATION_MIXED`：结构分化 / 风格轮动；
- `BALANCED`：整体较均衡。

`regime.breadth_estimated=true` 时，应结合 `market_environment.confidence` 与 `breadth.sampling.sample_coverage_percent` 使用。

### 个股 driver attribution

每个 `detail_stock` 会在 `market_environment.targets.<code>` 下得到相对强弱和归因上下文。`driver_attribution.primary_driver` 的枚举为：

```text
MARKET          主要跟随 broad-market
STYLE           更接近可用的风格 proxy
SECTOR          更接近配置 group / 板块
IDIOSYNCRATIC   同时明显偏离市场和板块，个股自身因素占比更高
MIXED           多种驱动叠加或分离程度不足
UNKNOWN         数据不足
```

不要只读取 `primary_driver`。LLM 推荐同时读取：

```text
driver_attribution.confidence
driver_attribution.evidence.stock_change_percent
driver_attribution.evidence.market_reference_percent
driver_attribution.evidence.sector_reference_percent
driver_attribution.evidence.style_reference_percent
driver_attribution.evidence.stock_vs_market_percent
driver_attribution.evidence.stock_vs_sector_percent
driver_attribution.evidence.stock_vs_style_percent
driver_attribution.reason_codes
```

这样即使归因结果是 `MIXED`，模型仍可以根据实际差值自行判断“市场强、板块弱、个股相对板块抗跌”这类更细的语义。

## 实时价格保护规则

盘中优先级为：

```text
LIVE 实时报价
    ↓ 不可用
LIVE 当日分钟价
    ↓ 仍不可用
当前价 = unavailable
```

不会执行：

```text
实时源失败 → 读取旧 snapshot / 日 K / history cache 冒充现价
```

如果未来代码修改意外让历史来源进入实时 quote，`live_price_guard` 会将其视为 hard violation，并让该次工作流失败，而不是静默输出旧价格。

## 历史数据设计

主仓库的生成数据放在独立 `market-data` 分支：

```text
history/
├─ daily_k/
│  ├─ 002558.json
│  └─ 600795.json
├─ snapshots/
│  └─ YYYY-MM-DD/
│     └─ HHMMSS_run....json
└─ manifest.json
```

日 K 当前保留约 120 根用于增量校验和 60 日上下文计算。盘中历史快照会去掉重复的 60 根日 K 和大段分钟列表，只保存当时的关键分析状态。

## GitHub Actions 自检

开发分支上的 reusable workflow 有独立冒烟测试：

```text
.github/workflows/reusable-selftest.yml
```

安全/回归检查包括：

- live-price guard 故障注入测试；
- quote resilience 主源失败 / stale / 双源分歧选择测试；
- watchlist/config 大小、数量、重复 code、路径和 symlink 逃逸测试；
- GitHub Action SHA pinning 与 reusable workflow engine revision 测试；
- 腾讯 quote 模块自身 HTTPS-only、源码不包含明文 HTTP fallback 的回归测试；
- market environment 的 market / sector / idiosyncratic driver attribution、stale index 排除、breadth 估算语义和 schema 回归测试；
- Web 公共行情接口的 Cloudflare Rate Limiting binding 接线、fail-closed 行为和 2 秒短 TTL 去重缓存测试；
- reusable workflow 小型观察列表实跑并生成 artifact。

稳定分支还有独立：

```text
.github/workflows/v1-smoke.yml
```

每次 `v1` 晋升后会再次执行 safety tests 和 reusable workflow 冒烟测试。稳定版发布/维护规则见：

```text
docs/STABLE_V1.md
```

## Web 行情站

仓库仍保留 Web 行情查询站：

```text
https://uploaded-code-site.zhangjinhao949792.chatgpt.site
```

Web 部分基于 Vinext / Cloudflare Worker，与当前 GitHub Actions 行情分析管线可以并存。

公开的 `/` 和 `/quote` 路由使用 `PUBLIC_QUOTE_RATE_LIMITER` Cloudflare Rate Limiting binding。当前配置为每个 `CF-Connecting-IP + route` 在 60 秒窗口内最多 60 次；超过后返回 HTTP 429 和 `Retry-After`。如果 binding 缺失或调用异常，公共行情路由会 fail closed 返回 503，而不是静默退回无保护状态。

Cloudflare Rate Limiting binding 的计数由 Cloudflare 平台管理，不依赖单个 Worker isolate 的内存 Map；但它按 Cloudflare location 生效且是 eventually consistent，因此这是针对异常高频访问的基础滥用防护，不应当作精确计费或全局强一致计数器。

同一股票的上游行情另外在 Worker isolate 内做 2 秒 best-effort 短缓存并合并并发请求。这个 Map **只用于削减重复上游请求，不承担限流安全边界**；浏览器响应仍为 `no-store`，短缓存也不作为历史行情或当前价 fallback 来源。

本地开发 Web 部分需要 Node.js `>=22.13.0`：

```bash
npm install
npm run dev
```

构建检查：

```bash
npm run build
```

## 主要目录

```text
.github/workflows/                     GitHub Actions、reusable workflow、v1 smoke
.github/dependabot.yml                 GitHub Actions 安全更新跟踪
config/quote_watchlist.json           默认观察列表 / 板块分组
docs/STABLE_V1.md                     稳定分支发布与兼容策略
scripts/config_security.py            caller/watchlist 输入安全边界
scripts/prepare_reusable_config.py    reusable 配置解析入口
scripts/transport_security.py         HTTPS-only 行情传输额外防御层
scripts/test_config_security.py       配置攻击边界回归测试
scripts/test_workflow_security.py     Action pin / engine SHA / HTTPS 测试
scripts/realtime_quotes_watchlist.py  基础实时行情与分钟数据
scripts/realtime_quotes_watchlist_runner.py 组合运行入口
scripts/quote_resilience.py           东方财富 / 腾讯 quote 双源容错与一致性检查
scripts/test_quote_resilience.py      行情源故障与分歧选择测试
scripts/intraday_metrics.py           分时结构指标
scripts/daily_k_context.py            日 K 背景与支撑压力
scripts/history_store.py              历史缓存与盘中快照
scripts/live_price_guard.py           实时价格来源保险
scripts/market_environment.py         市场环境 / 风格 / 个股驱动归因
scripts/market_breadth_source.py      全市场 breadth 全量 / 系统样本采集
scripts/test_market_environment.py    市场环境与估算语义回归测试
vite.config.ts                        Cloudflare Worker binding / rate-limit 配置
worker/index.ts                       Worker 入口及 binding 注入
worker/abuse-protection.js            Cloudflare 限流调用 / 2 秒上游去重缓存
worker/stock-quote.js                 Web 行情入口
tests/worker-abuse-protection.test.mjs Web 滥用防护测试
app/                                  Web 页面
```

## 说明

该项目输出的是行情数据与分析上下文，不构成投资建议。实时行情接口可能出现上游限流、502、数据延迟等情况，因此程序会保留数据源、时间戳、新鲜度、双源一致性和降级状态，供上层分析判断是否可以使用。