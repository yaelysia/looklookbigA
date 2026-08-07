# looklookbigA

一个面向 A 股盘中分析的实时行情与结构化快照工程。项目最初是简单的行情查询站，现在已经扩展为可以被 GitHub Actions / ChatGPT / 其他自动化流程复用的行情采集与分析底座。

当前默认重点跟踪巨人网络（002558）和国电电力（600795），并配置了一组游戏板块对照股；观察列表本身是可配置的，并不限制只能查询这些股票。

## 当前能力

- **重点标的实时行情**：现价、开高低、均价、涨跌幅、成交额和时间戳。
- **当日分钟线**：腾讯分钟行情，生成 5 / 15 / 30 分钟动量、VWAP 位置、日内位置和量能强度。
- **板块对照组**：支持轻量批量行情与组内均值、中位数、涨跌家数、领涨领跌、目标股相对强弱。
- **20～60 日日 K 背景**：MA5 / MA10 / MA20 / MA60、ATR14、5 / 10 / 20 日高低、20 日收益、日线 swing high / low。
- **支撑 / 压力上下文**：综合均线、昨日 OHLC、近期高低和日线拐点生成可解释的候选价位及共振强度。
- **历史缓存**：主仓库使用独立 `market-data` 分支持久保存日 K 和轻量盘中快照；同一交易阶段重复分析时日 K 可以做到 0 次网络请求。
- **实时价格保险**：历史缓存、历史快照和日 K 数据被禁止进入 `quote.latest`。盘中实时 quote 失效时，只允许降级到仍然 LIVE 的当日分钟价；两者都不新鲜时当前价直接标记不可用。
- **结构化产物**：每次运行生成 `snapshot.json`，当前 schema 会包含实时行情、分时结构、日 K 背景、板块对照、历史缓存状态和实时价格保护状态。

## 数据源与实时性

目前主要使用：

- 东方财富：重点标的最新 quote、指数等实时行情；
- 腾讯：分钟线、批量轻量行情、前复权日 K；
- 东方财富日 K：腾讯日 K 不可用时的备用源；
- `market-data` / GitHub Actions cache：**仅用于历史日 K 与历史分析上下文，不作为实时现价来源**。

盘中数据会携带 `market_time_cst`、`lag_seconds` 和 `freshness`。重点标的还会生成 `current_price_guard`，用于明确记录本次当前价来自实时 quote、实时分钟线，还是已经不可用。

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

`detail_codes` 会抓实时 quote、分钟线、日 K 和完整分析上下文；`light_codes` / group member 主要用于批量板块对照，适合一次放十几到几十只股票。

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

调用者自己的仓库只需要增加一个很小的 workflow，例如：

```yaml
name: A-share snapshot

on:
  workflow_dispatch:

jobs:
  quotes:
    uses: yaelysia/looklookbigA/.github/workflows/reusable-a-share-quotes.yml@master
    with:
      source_ref: master
      enable_history_cache: true
```

运行结果同样会产生 `realtime-snapshot` artifact，其中包含 `snapshot.json`。

### 使用调用者自己的观察列表

推荐在调用者仓库放置：

```text
config/quote_watchlist.json
```

reusable workflow 默认会优先读取这个文件。如果调用者仓库没有该文件，则使用 looklookbigA 自带的默认配置。

也可以直接传入 JSON：

```yaml
jobs:
  quotes:
    uses: yaelysia/looklookbigA/.github/workflows/reusable-a-share-quotes.yml@master
    with:
      source_ref: master
      config_json: >-
        {"detail_codes":["300750"],"light_codes":["002594"],"groups":{},"max_total_codes":20}
```

### reusable workflow 历史缓存

外部仓库默认使用 GitHub Actions cache 保存日 K 历史，因此不要求调用者创建 `market-data` 分支，也不需要 `contents: write` 权限。

```yaml
with:
  enable_history_cache: true
  cache_namespace: default
```

第一次运行会初始化历史，之后同一交易阶段通常可以直接命中缓存。跨交易阶段只校验最近少量 K 线；检测到前复权变化或历史缺口时才会完整刷新。

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
└─ live_price_guard
```

其中：

- `quote`：联网获得的当前行情；
- `intraday`：分时结构和最终可用当前价来源；
- `daily_context`：历史日 K 指标及支撑压力；
- `history`：缓存状态和历史快照位置；
- `live_price_guard`：实时价格源安全检查。

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

reusable workflow 有独立的冒烟测试：

```text
.github/workflows/reusable-selftest.yml
```

修改 reusable workflow、核心行情脚本或实时价格保护逻辑时，会调用 reusable workflow 本身跑一组小型观察列表，避免出现“主仓库能跑、外部调用方式已经坏掉”的情况。

## Web 行情站

仓库仍保留早期的 Web 行情查询站：

```text
https://uploaded-code-site.zhangjinhao949792.chatgpt.site
```

Web 部分基于 Vinext / Cloudflare Worker，与当前 GitHub Actions 行情分析管线可以并存。

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
.github/workflows/                 GitHub Actions 与 reusable workflow
config/quote_watchlist.json       默认观察列表 / 板块分组
scripts/realtime_quotes_watchlist.py  基础实时行情与分钟数据
scripts/realtime_quotes_watchlist_runner.py  组合运行入口
scripts/intraday_metrics.py       分时结构指标
scripts/daily_k_context.py        日 K 背景与支撑压力
scripts/history_store.py          历史缓存与盘中快照
scripts/live_price_guard.py       实时价格来源保险
app/                              Web 页面
worker/                           Cloudflare Worker / Web 行情逻辑
```

## 说明

该项目输出的是行情数据与分析上下文，不构成投资建议。实时行情接口可能出现上游限流、502、数据延迟等情况，因此程序会保留数据源、时间戳、新鲜度和降级状态，供上层分析判断是否可以使用。
