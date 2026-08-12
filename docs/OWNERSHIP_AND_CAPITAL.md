# 股权 / 股本 / 股东结构上下文（ownership_and_capital v1）

`detail_stocks.<code>.ownership_and_capital` 提供可追溯的公司所有权与资本结构事实。它只描述披露结构，不输出利好/利空、买卖建议、价格预测或估值结论。

## 执行与来源

- `FULL`：读取东方财富的股本、控制关系、前十大股东、机构持仓和股东户数，并复用巨潮资讯官方事件缓存。
- `INTRADAY_FAST`：不增加上述低频网络请求，相关 section 显式为 `DEFERRED`。
- 东方财富数据为 `PRIMARY_PROVIDER`；巨潮资讯公告为 `OFFICIAL`；派生趋势和信号继承输入来源等级。
- 每个 section 都有 `status / as_of_date / metadata / provenance`。报告期持仓与户数明确标记披露滞后，不能当作实时状态。

## Snapshot 结构

```text
detail_stocks.<code>.ownership_and_capital
├─ share_structure
├─ controllers
├─ top_holders
├─ institutional_holdings
├─ shareholder_count
├─ buyback_and_holder_plans
├─ unlocks
├─ pledges_and_capital_tools
├─ valuation_share_semantics
├─ structural_signals
├─ metadata
└─ provenance

detail_stocks.<code>.upcoming_events
└─ events[]  # 复用稳定 event_id 的未来解禁事项
```

### 股本与控制关系

`share_structure.values` 区分总股本、上市 A 股、无限售、限售和自由流通口径；比例只在分子分母均存在时派生。`history` 保留最多 12 个有日期的披露点，并输出相邻披露变化和结构趋势。

`controllers` 只接受供应商明确声明的 `sjkzr / kggd`。最大股东不会被推断成控股股东；一致行动关系也必须有明确 provider marker 才汇总。`control_change` 需要两个有日期的 provider-declared 关系期，否则为 `UNKNOWN`。控股股东最近持股变化只通过名称精确匹配前十大股东披露行。

### 股东、机构与户数趋势

- `top_holders`：最近最多 4 个报告期的总股本/流通股前十大股东、持股数、比例、变化和 provider-declared 类型；集中度趋势比较最近两个可比报告期。
- `institutional_holdings`：最近最多 4 个披露期的机构数、持股数、总股本/流通股比例、基金比例和相邻变化。Q1/Q3 基金披露可能不完整，聚合字段缺失时不会把部分和伪装成完整总量。
- `shareholder_count`：原始历史、相邻变化以及 3/6/12 月 on-or-before 基线；趋势仅为 `SHAREHOLDER_COUNT_RISING / SHAREHOLDER_COUNT_FALLING / STABLE / VOLATILE / UNKNOWN` 的结构描述。

### 计划、解禁、质押与资本工具

回购、增持、减持只在明确 `IN_PROGRESS` 或明确执行起止日期时进入 `confirmed_active`；普通 `OPEN` 公告不会被提升为正在执行。剩余金额/股份只从明确计划区间与已完成量计算。

解禁输出当前限售股、未来 7/30/90/180 个自然日累计窗口、总股本/流通股比例和历史重要事项。没有明确解禁股数时，公告百分比保留为 unscoped，不强行指定分母。

`pledges_and_capital_tools` 复用官方 `PLEDGE / CONVERTIBLE_BOND / PREFERRED_SHARES / REFINANCING` 事件。标题中的百分比默认 unscoped；只有明确命名且口径确定的质押或潜在稀释比例才进入对应结构字段。

### 结构信号

`structural_signals.assessments` 始终覆盖以下代码，并以 `CONFIRMED / NOT_CONFIRMED / UNKNOWN` 表示证据状态：

```text
CONTROLLING_HOLDER_INCREASING
CONTROLLING_HOLDER_DECREASING
OWNERSHIP_CONCENTRATION_RISING
INSTITUTIONAL_HOLDING_RISING
SHAREHOLDER_COUNT_RISING
BUYBACK_IN_PROGRESS
MAJOR_UNLOCK_UPCOMING
CAPITAL_STRUCTURE_STABLE
```

只有 `CONFIRMED` 会进入 `signals[]`，且每项都携带日期、数值或 event_id 证据。信号不包含行情方向或交易含义。

## 跨层集成

- `valuation_share_semantics` 并列保存 Eastmoney `f20/f21` 市值观察值及有日期的股本分母，只声明口径，不做估值推断。
- `upcoming_events.events[]` 复用解禁的稳定 `event_id`，供日历/提醒层直接消费。
- exact history 保留 ownership 与 upcoming 节点；`changes_since_previous.stocks.<code>.ownership_and_capital_change` 只报告新披露期、同期间修订或持久事件状态变化，忽略 `fetched_at` 等采集噪声。没有可比 ownership baseline 时不声称发生变化。
- 顶层 `ownership_and_capital_summary` 汇总覆盖、section contract 和全部信号代码。

## 验证入口

唯一完成验证入口为：

```bash
python3 scripts/test_ownership_capital_completion.py
```

该入口聚合基础、股东/机构、股东户数、计划/解禁及完成集成测试，并被 required、reusable selftest、v1 smoke 与 realtime 工作流目录共同执行。
