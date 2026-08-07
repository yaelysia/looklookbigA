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
- **20～60 日日 K 背景**：MA5 / MA10 / MA20 / MA60、ATR14、5 / 10 / 20 日高低、20 日收益、日线 swing high / low。
- **支撑 / 压力上下文**：综合均线、昨日 OHLC、近期高低和日线拐点生成可解释的候选价位及共振强度。
- **历史缓存**：主仓库使用独立 `market-data` 分支持久保存日 K 和轻量盘中快照；同一交易阶段重复分析时日 K 可以做到 0 次网络请求。
- **实时价格保险**：历史缓存、历史快照和日 K 数据被禁止进入 `quote.latest`。盘中实时 quote 失效时，只允许降级到仍然 LIVE 的当日分钟价；两者都不新鲜时当前价直接标记不可用。
- **Reusable workflow 安全边界**：版本绑定到精确 workflow SHA、调用者配置做路径/大小/数量校验、第三方 Actions 固定 commit SHA、行情传输禁止 HTTP 降级。
- **公共 Web 接口基础滥用防护**：异常高频请求返回 429；极短 TTL 服务端行情去重降低重复上游请求，同时浏览器端仍保持 `no-store`。
- **结构化产物**：每次运行生成 `snapshot.json`，包含实时行情、分时结构、日 K 背景、板块对照、历史缓存状态、行情源容错状态和实时价格保护状态。

## 数据源与实时性

目前主要使用：

- 东方财富：重点标的最新 quote、指数等实时行情的主源；
- 腾讯：重点股 / 指数备用 quote、分钟线、批量轻量行情、前复权日 K；
- 东方财富日 K：腾讯日 K 不可用时的备用源；
- `market-data` / GitHub Actions cache：**仅用于历史日 K 与历史分析上下文，不作为实时现价来源**。

行情传输只允许 HTTPS。腾讯 quote 的 HTTPS 请求失败时会继续走其他已验证的 HTTPS source / error 路径，不会降级成明文 HTTP。

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
├─ history
├─ live_price_guard
└─ quote_resilience
```

其中：

- `quote`：联网获得的当前行情，并包含该 quote 的双源容错 / 一致性元数据；
- `intraday`：分时结构和最终可用当前价来源；
- `daily_context`：历史日 K 指标及支撑压力；
- `history`：缓存状态和历史快照位置；
- `live_price_guard`：实时价格源安全检查；
- `quote_resilience`：本次运行的 provider 使用情况、fallback 数量、双源分歧和不可用统计。

历史日 K 的 `source` 可能显示为 `History cache (...)`，这是正常的；**只有 `daily_context` 可以使用历史缓存，`quote.latest` 不允许使用任何 History / cache / snapshot 来源。**

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
- HTTPS transport 不允许 HTTP fallback 的回归测试；
- Web 公共行情接口限流和短 TTL 去重缓存测试；
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

公开的 `/` 和 `/quote` 路由具有基础滥用防护：同一客户端、同一路由在 60 秒窗口内最多接受 60 次请求，超过后返回 HTTP 429 和 `Retry-After`；同一股票的上游行情会在 Worker 进程内做 2 秒短缓存并合并并发请求。浏览器响应仍为 `no-store`，因此该短缓存只用于降低重复上游压力，不作为历史行情源。

这是一层不增加 KV / Durable Objects 等部署依赖的基础应用层防护。如果以后公开流量明显增大，可进一步在部署层接 Cloudflare 原生 Rate Limiting / WAF。

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
scripts/transport_security.py         HTTPS-only 行情传输策略
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
worker/abuse-protection.js            Web 接口限流 / 短 TTL 请求去重
worker/stock-quote.js                 Web 行情入口
tests/worker-abuse-protection.test.mjs Web 滥用防护测试
app/                                  Web 页面
```

## 说明

该项目输出的是行情数据与分析上下文，不构成投资建议。实时行情接口可能出现上游限流、502、数据延迟等情况，因此程序会保留数据源、时间戳、新鲜度、双源一致性和降级状态，供上层分析判断是否可以使用。
