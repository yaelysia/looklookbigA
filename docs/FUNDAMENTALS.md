# 基本面 / 财务趋势上下文（fundamentals v1）

`fundamentals` 的目标不是给 LLM 一个“基本面好/坏”的单一标签，而是把公司已披露财务报表转成**可追溯、可验证口径、可比较的结构化上下文**。

## 数据源与信任等级

v1 通过 Eastmoney 数据中心读取公司已披露财务报表数据，并保留 provider URL / fetch time / report publish time / first seen time。

因此：

```text
source = Eastmoney
Source Trust = B
```

报表内容本身来源于公司披露，但实际传输与接口提供者是市场数据服务商，所以不会把这一路数据伪装成 Trust A 官方直连。

未来如果接入交易所 / CNINFO 原始 XBRL 或官方财报结构化源，可以并行增加 Trust A 数据层，而不是覆盖 provenance。

## FAST / FULL 边界

财务数据是低频数据，不应该阻塞盘中决策。

```text
INTRADAY_FAST
→ 只读取 .market-data/history/fundamentals/<code>.json
→ 0 个财务网络请求

FULL
→ 并发刷新 main / income / balance / cashflow 四张报表
→ 更新 fundamentals cache
```

没有缓存时 FAST 输出：

```text
status = UNAVAILABLE
cache.state = MISS
```

但不会影响 realtime quote / minute 的 `critical_ready`。

## Snapshot 结构

```text
detail_stocks.<code>.fundamentals
├─ status
├─ latest_report_period_end
├─ latest_published_at
├─ reported_periods[]
├─ single_quarters[]
├─ ttm
├─ trends
├─ cashflow_quality
├─ balance_sheet_growth
├─ profitability_context
├─ divergence_signals[]
├─ refresh_trigger
├─ peer_comparison
├─ coverage
├─ provider_health
├─ metadata
└─ provenance
```

顶层：

```text
fundamentals_summary
```

## 报告期原值

`reported_periods[]` 保存公司披露口径，不把累计值误称为单季度：

```text
reported_scope = REPORTED_CUMULATIVE
```

主要字段包括：

```text
income
├─ revenue
├─ parent_net_profit
├─ adjusted_net_profit
├─ operating_profit
├─ gross_profit
├─ revenue_yoy_percent_reported
└─ parent_net_profit_yoy_percent_reported

profitability
├─ gross_margin_percent_reported
├─ weighted_roe_percent_reported
├─ operating_margin_percent_derived
└─ net_margin_percent_derived

cashflow
├─ operating_cash_flow
├─ investing_cash_flow
├─ financing_cash_flow
└─ operating_cash_flow_to_parent_profit

balance_sheet
├─ total_assets
├─ total_liabilities
├─ equity
├─ cash
├─ receivables
├─ inventory
├─ goodwill
├─ interest_bearing_debt
├─ debt_to_assets_percent
└─ cash_to_interest_bearing_debt
```

字段不存在时保持 `null`，不通过别的近似字段静默填充。

## 单季度归一化

只有能够由已披露累计口径**确定性恢复**时，才输出：

```text
reported_scope = NORMALIZED_SINGLE_QUARTER
normalization.verified = true
```

规则固定为：

```text
Q1 = Q1 cumulative
Q2 = H1 cumulative - Q1 cumulative
Q3 = Q3 cumulative - H1 cumulative
Q4 = FY cumulative - Q3 cumulative
```

注意依赖是局部的。例如缺 Q1 时不能恢复 Q2，但如果 H1 和 Q3 都存在，Q3 仍可由 `Q3-H1` 正确恢复。

没有必要的累计前序时，该季度直接缺失，不猜测、不平均拆分。

## TTM

`TTM` 只有在存在**连续四个 verified single quarter** 时才生成：

```text
TTM = sum(last 4 consecutive normalized single quarters)
```

任何一个季度缺失或不连续：

```text
ttm.status = UNAVAILABLE
```

这样不会把累计半年报 / 年报重复相加。

## 财务趋势

v1 对最近已验证的季度序列生成确定性趋势状态：

```text
ACCELERATING
IMPROVING
STABLE
SLOWING
DETERIORATING
VOLATILE
UNKNOWN
```

覆盖：

```text
revenue
parent_net_profit
adjusted_net_profit
net_margin
operating_cash_flow
weighted_roe
```

趋势只是时序描述，不等于估值或交易建议。

## 现金流质量

`cashflow_quality` 使用单季度归母净利润与经营现金流做会计质量上下文：

```text
STRONG
ADEQUATE
WEAK
DIVERGENT
CASHFLOW_BETTER_THAN_EARNINGS
UNKNOWN
```

例如“利润为正但经营现金流为负”会进入 `DIVERGENT`，同时输出 ratio、reason codes 和 semantic note。

## 资产负债趋势

`balance_sheet_growth` 只做**同报告期同比**，避免季节性错配：

```text
2026H1 vs 2025H1
2026Q3 vs 2025Q3
```

包括：

```text
total_assets
total_liabilities
equity
cash
receivables
inventory
goodwill
interest_bearing_debt
```

## 盈利能力口径

v1 区分 reported 与 derived：

- 毛利率：官方/供应商报表字段存在时使用 reported；
- 加权 ROE：reported；
- 单季度净利率：仅在 verified single-quarter revenue/profit 都存在时 derived；
- ROA：v1 **明确不计算**，因为没有稳定的平均资产分母口径。

ROA 输出：

```text
status = UNAVAILABLE
reason = NO_RELIABLE_AVERAGE_ASSET_DENOMINATOR_IN_V1
```

宁可缺值，也不拿期末资产伪造平均资产收益率。

## 财务背离

v1 只生成确定性、带证据的背离：

```text
PROFIT_UP_CASHFLOW_DOWN
MARGIN_DOWN_REVENUE_UP
REVENUE_UP_RECEIVABLES_FASTER
LEVERAGE_RISING_CASH_FALLING
```

每条 signal 保存参与计算的指标，LLM 再结合行业、周期和事件解释含义；底层不直接输出“利好 / 利空”。

## 与公告事件联动

公告层已经有 `PERIODIC_REPORT`。fundamentals 会检查：

```text
最近 PERIODIC_REPORT published_at
vs
fundamentals cache fetched_at
```

若公告晚于财务 cache，则：

```text
refresh_trigger.recommended = true
reason = PERIODIC_REPORT_EVENT_AFTER_CACHE
```

这让盘中 FAST 即使只读低频 cache，也能告诉 LLM：**现在可能有新财报，但本轮财务缓存尚未 FULL 刷新。**

## first_seen / Freshness SLA

财务 report version 以：

```text
report_period_end + published_at
```

作为版本键，第一次 FULL 看见时写入稳定 `first_seen_at`。后续同版本 refresh 不会重置 first_seen。

`FUNDAMENTALS` 使用 `DISCOVERY_LAG` SLA：

```text
target = 24h
hard limit = 72h
```

这是“系统首次发现披露”的工程 SLA，不是报表本身的会计有效期。

## changes_since_previous

```text
changes_since_previous.stocks.<code>.fundamentals
```

区分：

```text
NEW_FINANCIAL_REPORT_PERIOD
SAME_PERIOD_FINANCIAL_VALUES_UPDATED
FUNDAMENTAL_TREND_CHANGED
NEW_FUNDAMENTAL_DIVERGENCE
FUNDAMENTAL_DIVERGENCE_CLEARED
```

新报告期不会把不同季度的金额直接当作“delta 可比”；同报告期后续修订才允许做字段级 before/after/delta。

## 历史状态

fundamentals 会进入现有 exact history baseline，因此下一轮可以比较财务趋势 / 报告期变化，同时继续受到 #45 的 exact previous-run barrier 与 monotonic writer 保护。

## Peer comparison

v1 明确：

```text
peer_comparison.status = DEFERRED_V1
```

原因是当前重点是给目标股补完整基础财务上下文；如果为了板块财务横比在每轮实时 pipeline 给 10～20 个 peer 各抓四张报表，会显著扩大低价值网络负担。

后续应做成独立低频 peer fundamentals cache，而不是进入盘中 critical path。
