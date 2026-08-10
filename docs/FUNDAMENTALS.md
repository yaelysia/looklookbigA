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

财务数据是低频数据，不应该要求盘中每轮重复联网获取。

```text
INTRADAY_FAST
→ 只读取 .market-data/history/fundamentals/<code>.json
→ 0 个财务网络请求

FULL
→ 并发刷新 main / income / balance / cashflow 四张报表
→ 更新 fundamentals cache
```

正常、完整且没有新 `PERIODIC_REPORT` 提示的 FAST cache hit 可以保持：

```text
status = CACHED
metadata.quality = PASS
metadata.quality_flags += FAST_CACHE_ONLY
```

`CACHED` 描述获取路径，不自动等于数据质量下降。只有 cache 不完整、新财报事件晚于 cache、provider regression / corruption 等真实质量条件才降级。

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

只有能够由已披露累计口径**确定性恢复报告期**时，才输出 `NORMALIZED_SINGLE_QUARTER` 对象：

```text
reported_scope = NORMALIZED_SINGLE_QUARTER
normalization.period_verified = true
normalization.verification_scope = PERIOD_ARITHMETIC_ONLY
```

规则固定为：

```text
Q1 = Q1 cumulative
Q2 = H1 cumulative - Q1 cumulative
Q3 = Q3 cumulative - H1 cumulative
Q4 = FY cumulative - Q3 cumulative
```

注意依赖是局部的。例如缺 Q1 时不能恢复 Q2，但如果 H1 和 Q3 都存在，Q3 仍可由 `Q3-H1` 正确恢复。

报告期可恢复并不代表该季度内每个财务字段都有值。因此 v1 同时输出字段级验证：

```text
normalization.verified_fields
├─ revenue
├─ parent_net_profit
├─ adjusted_net_profit
└─ operating_cash_flow

normalization.core_fields_verified
```

例如 H1 和 Q1 都存在、但 H1 的 `revenue=null` 时，Q2 这个季度对象仍可存在，但：

```text
normalization.period_verified = true
normalization.verified_fields.revenue = false
single_quarter.income.revenue = null
```

不会把“报告期存在”误当成“每个字段都已验证”。

## TTM

TTM 首先要求最近四个 `NORMALIZED_SINGLE_QUARTER` 的季度标签连续；随后再逐字段检查四个季度是否都有可验证值。

核心 TTM 字段：

```text
revenue
parent_net_profit
adjusted_net_profit
operating_cash_flow
```

状态合同：

```text
OK
→ 四个连续季度存在，并且所有核心 TTM 字段在四个季度均完整、可验证

PARTIAL
→ 四个连续季度存在，但至少一个核心字段缺失；可计算字段仍保留自己的值与 availability

UNAVAILABLE
→ 连续季度不足 / 不连续，或核心字段整体不可计算
```

每个核心字段都有：

```text
ttm.field_availability.<field>.status
ttm.field_availability.<field>.verified_quarter_count
ttm.field_availability.<field>.required_quarter_count
ttm.field_availability.<field>.source_periods
```

只有：

```text
ttm.status = OK
```

时：

```text
coverage.ttm_available = true
```

这样不会出现 `ttm.status=OK` 但核心字段为 `null` 的伪完整状态。

## 财务趋势：按数据口径分类

不同财务指标不能共用“数值上涨 = 改善”的单一算法。v1 明确分三类。

### 1. 已标准化单季度流量 / 单季度派生指标

收入、利润、扣非利润、经营现金流、单季度净利率等使用 `NORMALIZED_SINGLE_QUARTER` 序列，可以使用：

```text
ACCELERATING
IMPROVING
STABLE
SLOWING
DETERIORATING
VOLATILE
UNKNOWN
```

同时保留 series / evidence / method。

### 2. 累计披露比例指标

加权 ROE、reported gross margin 等不能把：

```text
FY level → next-year Q1 level
```

当作相邻季度趋势，因为二者累计窗口不同。

这类指标只建立同报告类型同比比较：

```text
2026Q1 vs 2025Q1
2026H1 vs 2025H1
2026Q3 vs 2025Q3
2026FY vs 2025FY
```

序列项明确保存：

```text
value_percent
prior_year_value_percent
yoy_delta_pp
comparison = SAME_REPORT_KIND_PRIOR_YEAR
```

并在 trend 中声明：

```text
comparability = SAME_REPORT_KIND_PRIOR_YEAR_ONLY
```

因此不会再把 `2025FY ROE → 2026Q1 ROE` 的 level change 直接解释为 deterioration/improvement。

### 3. 时点指标

资产负债率属于报告期末时点指标，可以按时间连续观察，但趋势状态只描述方向，不做价值判断：

```text
RISING
FALLING
STABLE
VOLATILE
UNKNOWN
```

并声明：

```text
comparability = POINT_IN_TIME_SEQUENTIAL
```

因此资产负债率 `16.4% → 18.7% → 19.86%` 会输出 `RISING`，不会输出 `IMPROVING`。

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

## 资产负债同比

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

- 毛利率：官方/供应商报表字段存在时使用 reported；跨报告类型的 level 不直接串成季度趋势，趋势使用同报告类型同比；
- 加权 ROE：reported；趋势使用同报告类型同比；
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

## 财报 cache 单调性

FULL provider 返回成功并且非空，不代表数据窗口一定没有回退。

fundamentals cache 对每个 report class（main / income / balance / cashflow）按 `report_period` 合并与去重，而不是直接用 fresh list 整表覆盖历史。

```text
provider latest > cache latest
→ ADVANCED / merge

provider latest == cache latest
→ SAME_PERIOD / merge non-null fields，并保留旧历史

provider latest < cache latest
→ PROVIDER_REPORT_WINDOW_REGRESSED
→ 保留较新的 cache
→ 不让持久化状态向旧报告期倒退
```

如果 provider 同一个最新报告期仍存在，但临时只返回更短的历史窗口，旧的历史报告期也不会被擦除。

回退或 report-class fallback 会保留之前成功 cache 的 `fetched_at`，并单独记录 refresh attempt / continuity 信息，避免失败尝试伪装成成功的新鲜刷新。

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
FINANCIAL_REPORT_PERIOD_REGRESSED
FUNDAMENTAL_TREND_CHANGED
NEW_FUNDAMENTAL_DIVERGENCE
FUNDAMENTAL_DIVERGENCE_CLEARED
```

`NEW_FINANCIAL_REPORT_PERIOD` 只允许严格的：

```text
after_period > before_period
```

如果观测到：

```text
after_period < before_period
```

会标记 `FINANCIAL_REPORT_PERIOD_REGRESSED`，不会包装为新财报。

所有 feature-specific changes 完成后，runner 会统一执行最终 generic summary recount，因此：

```text
stocks.<code>.significance
summary.significant_changes / moderate_changes / minor_changes
```

保持一致，不再依赖每个 feature 自行维护通用计数。

## 历史状态

fundamentals 会进入现有 exact history baseline，因此下一轮可以比较财务趋势 / 报告期变化，同时继续受到 #45 的 exact previous-run barrier 与 monotonic writer 保护。

## Peer comparison

v1 明确：

```text
peer_comparison.status = DEFERRED_V1
```

原因是当前重点是给目标股补完整基础财务上下文；如果为了板块财务横比在每轮实时 pipeline 给 10～20 个 peer 各抓四张报表，会显著扩大低频网络负担。

后续应做成独立低频 peer fundamentals cache，而不是进入盘中 critical path。
