# 资金 / 成交行为上下文（capital_flow v1）

`capital_flow` 为 LLM 提供可审计的成交行为与资金上下文，但**不输出买卖指令，也不把第三方“主力资金”口径当作交易所事实**。

## 数据层级

每个字段必须明确落入以下语义之一：

```text
OBSERVED
DERIVED
OFFICIAL_DELAYED
VENDOR_ESTIMATE
```

### OBSERVED

来自现有行情 / 分钟数据可直接观察的事实，例如累计成交额、最近 1/5/15/30 分钟成交额、成交速度、价格上涨/下跌分钟对应的成交额与成交量、VWAP 上下方成交分布。

“上涨分钟成交额 / 下跌分钟成交额”只表示**该分钟价格相对上一分钟上涨/下跌时伴随的成交**，不等价于真实主动买入 / 主动卖出。

### DERIVED

项目基于 OBSERVED 数据确定性计算的上下文，例如：

```text
price_volume_confirmation
pressure
absorption
vwap_acceptance
peer_context
```

这些节点必须保留公式 / evidence / reason_codes / provenance。`pressure.buying` 与 `pressure.selling` 是方向性量价压力，不是资金净流入金额；`absorption` 是承接形态，不断言存在隐藏主力。

### OFFICIAL_DELAYED

融资融券属于披露型、低频数据。v1 从 Eastmoney 数据中心读取交易所披露语义的融资融券数据，因此：

```text
data_class = OFFICIAL_DELAYED
Source Trust = B
```

`OFFICIAL_DELAYED` 描述数据语义，不会把 Eastmoney 传输源提升成 Trust A。只有直接交易所 / 官方第一方端点才可以获得官方原始来源等级。

### VENDOR_ESTIMATE

v1 不使用“大单 / 超大单 / 主力净流入”等供应商估算值：

```text
vendor_estimate.status = NOT_USED
```

未来如增加，必须单独标记 provider、估算口径和 Trust，不得主导 OBSERVED / DERIVED 结论。

## Snapshot 结构

```text
detail_stocks.<code>.capital_flow
├─ status
├─ observed
│  ├─ turnover
│  ├─ volume_structure
│  └─ vwap_distribution
├─ derived
│  ├─ price_volume_confirmation
│  ├─ pressure
│  ├─ absorption
│  └─ vwap_acceptance
├─ official_delayed
│  └─ margin
├─ vendor_estimate
├─ peer_context
├─ metadata
└─ provenance
```

顶层同时提供：

```text
capital_flow_summary
```

它只汇总覆盖 / 状态，不生成单一“资金总分”。

## 成交速度

`turnover` 使用已经获取的 Tencent 分钟序列，不额外发起 minute 请求。最新分钟可能仍在累积，因此金额 / 成交结构尽量只使用已完成分钟。

至少包含：

```text
amount_today
volume_today
amount_1m / 5m / 15m / 30m
volume_1m / 5m / 15m / 30m
amount_rate_1m / 5m / 15m / 30m
amount_rate_baseline_20m
amount_rate_vs_baseline
turnover_acceleration
amount_rate_vs_previous_snapshot
```

累计成交额跨 session 不直接作为变化量比较。同行成交速度使用当前快照与上一 exact snapshot 的**同一 market session 累计成交额差 / 时间间隔**。

## 上涨 / 下跌成交结构

分别统计 full session、最近 30 分钟和最近 15 分钟：

```text
up_amount / down_amount / flat_amount
up_volume / down_volume / flat_volume
up_amount_share / down_amount_share / flat_amount_share
up_volume_share / down_volume_share / flat_volume_share
up_amount_vs_down_amount
up_volume_vs_down_volume
```

该结构描述量价相关性，不赋予逐笔买卖方向语义。

## 量价确认

确定性状态：

```text
UP_VOLUME_EXPANSION
UP_VOLUME_CONTRACTION
DOWN_VOLUME_EXPANSION
DOWN_VOLUME_CONTRACTION
NEUTRAL
UNKNOWN
```

v1 使用最近约 15 分钟价格变化以及 5 分钟成交额速度相对 20 分钟基线判定，并输出阈值和 reason codes。

## Directional pressure

v1 使用以下可复核成分：

```text
上涨 / 下跌成交额占比差
价格相对 VWAP
15 分钟价格趋势
```

输出：

```text
buying: 0..100
selling: 0..100
net_bias: BUY | SELL | BALANCED | UNKNOWN
confidence
components
formula
evidence
reason_codes
```

这里的 `buying / selling` 是方向性压力得分，不是人民币净流量。

## 承接 / absorption

最近约 30 个完成分钟中寻找“下跌 → 局部低点 → 恢复”的结构，并结合：

```text
局部回撤幅度
低点附近成交额相对前序基线
低点后的价格恢复比例
是否继续创新低
当前是否回到 VWAP 上方
```

输出：

```text
STRONG | MODERATE | WEAK | NONE | UNKNOWN
```

以及 score / evidence / reason_codes。该字段只是形态上下文。

## VWAP 行为

统计 VWAP 上 / 下 / 附近成交额占比，并结合最近分钟位置生成：

```text
ACCEPTED_ABOVE_VWAP
ACCEPTED_BELOW_VWAP
RECLAIMING_VWAP
REJECTED_AT_VWAP
OSCILLATING_AROUND_VWAP
UNKNOWN
```

## 融资融券

FULL 可以刷新最近融资融券记录并写入：

```text
.market-data/history/capital_flow/margin/<code>.json
```

主要字段：

```text
as_of_trade_date
financing_balance
financing_buy_amount
financing_repay_amount
financing_net_buy_amount
securities_lending_balance
margin_balance
financing_balance_change_1d / 5d / 20d
price_financing_relation_5d
```

FAST 不发融资网络请求，只读取已有缓存。缓存存在但无法证明“当前最新可披露 session”时：

```text
status = CACHED
Freshness SLA = UNMEASURED
```

而不是伪造 `MET`。无缓存时为 `UNAVAILABLE`，capital_flow 可以 `DEGRADED`，但不会影响实时 current-price critical readiness。

## 同行 / 板块资金同步性

`peer_context` 复用现有 group universe。对当前与上一 snapshot 的同 session 累计成交额计算单位时间增量，再得到：

```text
relative_capital_strength
rank
peer_count
sector_sync
sector_active_direction
```

同时保留：

```text
peer_universe_signature
previous_peer_universe_signature
comparability
```

peer universe 变化时 relative change 不可比较，变化层必须返回 non-comparable，而不是制造资金排名增强 / 减弱。

## changes_since_previous

每只 detail stock 的：

```text
changes_since_previous.stocks.<code>.capital_flow
```

可描述：

- 成交速度及相对基线变化；
- pressure bias 切换；
- absorption 出现 / 消失 / 状态切换；
- 量价确认状态变化；
- VWAP acceptance 变化；
- 在 peer universe 可比时的相对资金强度变化；
- 仅在 `as_of_trade_date` 前进时报告新的融资披露变化。

## FAST 性能合同

资金层不能破坏 `INTRADAY_FAST`：

```text
minute / quote：复用已有数据
margin：cache-only
peer：复用 current + previous exact snapshot
derived：本地计算
```

因此 FAST 不新增资金类网络依赖。实际 branch benchmark 中，2 个 detail stock 的 `capital_flow` 本地阶段约 10 ms，整条 decision-ready 仍保持秒级。

## 数据质量

所有主要子节点都进入现有 metadata contract：

```text
source
source_tier
trust
fetched_at
data_time
freshness
freshness_sla
quality
quality_flags
provenance
```

缺失数据必须显式 `PARTIAL / DEGRADED / UNAVAILABLE / UNMEASURED`，不允许用空值表示“中性资金”。
