# 未来事件日历（upcoming_events v1）

`detail_stocks.<code>.upcoming_events` 是面向 LLM 的事实型未来事件上下文。它只整理当前快照中已经存在、且日期语义能够被来源事实证明的公司事件；不预测新闻、不猜测下一次财报日期，也不输出利好/利空、仓位或买卖结论。

该能力将 `snapshot.json` schema floor 提升到 `18`，并设置：

```text
features.upcoming_events = "v1"
```

## 执行位置与数据边界

事件日历不增加新的盘中网络依赖。runner 在已有公司事件和 `ownership_and_capital` 完成后执行：

```text
company events / ownership facts
        ↓
upcoming_events core
        ↓
upcoming_events_calendar
        ↓
changes_since_previous
        ↓
upcoming_events_changes / upcoming_events_quality
```

当前事实来源包括：

- `detail_stocks.<code>.events.upcoming` 中能够证明具体日期语义的官方公司事件；
- `detail_stocks.<code>.ownership_and_capital.unlocks` 的未来解禁事实；
- `detail_stocks.<code>.ownership_and_capital.buyback_and_holder_plans` 中显式披露的回购、增持、减持执行窗口或窗口边界。

FAST/FULL 都只消费当前快照里已经形成的这些事实。`upcoming_events` 本身不会为了补日历再请求外部 provider。

## 日期语义与 fail-closed 规则

事件对象使用 `date_certainty` 明确区分四种合同语义：

```text
CONFIRMED_DATE
CONFIRMED_RANGE
EXPECTED_WINDOW
UNKNOWN
```

同时输出 `date_confidence`。当前规则如下：

- `CONFIRMED_DATE`：来源能证明一个精确未来日期，或一个已经开始的确认区间只剩仍在未来的明确结束边界。默认 `date_confidence=HIGH`，`date_end=null`。
- `CONFIRMED_RANGE`：来源存在有序、明确的官方起止边界，并且整个区间仍在未来。输出 `event_date=<start>`、`date_end=<end>`，默认 `date_confidence=HIGH`。
- `EXPECTED_WINDOW`：只允许来源本身明确声明预期窗口，且同时满足有序双端日期、`source_tier=OFFICIAL`、`window_semantics_source=OFFICIAL_EXPECTED_WINDOW`。输出 `event_date=<start>`、`date_end=<end>`，并强制降低到 `date_confidence=MEDIUM`。
- `UNKNOWN`：不会进入任何带日期的 upcoming bucket。无法证明日期、未知窗口、倒置区间或缺乏官方依据的 expected window 都 fail closed，并在 metadata 中累计排除计数。

范围事件的 `days_until_event` 和 bucket 都以 `event_date`（范围起点）为锚点，`date_end` 只表达范围终点，不会额外制造第二条事件。若一个 `CONFIRMED_RANGE` 在当前 `as_of_date` 之前已经开始但尚未结束，则不再把已过去的起点作为 upcoming 事件，而只保留仍在未来的明确结束边界，并按 `CONFIRMED_DATE` 输出。

当前允许来源包括：

- 解禁：结构化事实里的 `unlock_date`，且与事件日期语义一致；
- 回购/增减持：明确的 `window_start_date / window_end_date`；完整未来双端区间优先表达为 `CONFIRMED_RANGE`，已开始区间保留未来结束边界，只有单个可用未来边界时才输出对应 start/end 事件；
- 官方预期执行窗口：必须满足上面的 `OFFICIAL_EXPECTED_WINDOW` 证据约束才允许输出 `EXPECTED_WINDOW`；
- 分红：公告标题明确标注的股权登记日、除权除息日、现金红利发放日；
- 股东大会：标题中明确的召开日期/时间语义；
- 复牌：标题中明确的复牌日/日期/时间语义；
- 定期报告、业绩快报、业绩预告：只有标题明确标注披露日/预约披露日时才输出 `EARNINGS_RELEASE`。

普通公告 `effective_date`、标题中没有业务标签的日期、历史财报发布时间和其他不能证明未来业务语义的日期全部 fail closed。被排除的可疑日期通过 metadata / quality 留痕，而不是被静默猜测。

## Snapshot 结构

```text
detail_stocks.<code>.upcoming_events
├─ status
├─ as_of_date
├─ nearest
├─ next_7d[]
├─ next_30d[]
├─ next_90d[]
├─ later[]
├─ calendar_summary
├─ metadata
└─ provenance

upcoming_events_summary
├─ status
├─ detail_stock_count
├─ status_by_code
├─ implemented_sources
├─ date_policy
├─ quality_by_code
├─ quality_flag_counts
├─ metadata
└─ provenance
```

四个窗口是**非重叠自然日桶**：

```text
next_7d   = 0..7 天
next_30d  = 8..30 天
next_90d  = 31..90 天
later     = >90 天
```

单日事件以 `event_date` 分桶；`CONFIRMED_RANGE / EXPECTED_WINDOW` 同样以范围起点 `event_date` 分桶。`date_end` 不参与第二次分桶。`calendar_summary.next_30d_event_count` 等累计指标会跨桶统计到对应阈值，因此不要把累计指标与单桶数组长度直接等同。

## 事件对象

事件对象的稳定核心为：

```json
{
  "event_id": "upcoming:<source-event>:<type>:<date-or-range>",
  "event_type": "BUYBACK_EXECUTION_WINDOW",
  "title": "...",
  "event_date": "2026-08-20",
  "date_end": "2026-09-20",
  "date_certainty": "CONFIRMED_RANGE",
  "date_confidence": "HIGH",
  "days_until_event": 7,
  "importance": "MEDIUM",
  "status": "UPCOMING",
  "source_event_id": "...",
  "source_relations": [],
  "details": {},
  "trading_day_context": {},
  "overlap_context": {}
}
```

精确单日事件使用 `date_end=null`。官方 expected window 使用同样的起止字段，但 `date_certainty=EXPECTED_WINDOW` 且 `date_confidence=MEDIUM`。

`event_id` 便于快照内引用，但跨快照变化比较优先使用稳定 `source_event_id + event_type`。这是因为日期修订会改变包含日期/范围的 `event_id`；比较层会把 `event_date` 或 `date_end` 修订识别为 `EVENT_DATE_CHANGED`，而不是伪造一条删除和一条新增。

## 事件类型覆盖

当前明确支持的高价值日期语义包括：

```text
UNLOCK
BUYBACK_EXECUTION_WINDOW
HOLDER_INCREASE_EXECUTION_WINDOW
HOLDER_DECREASE_EXECUTION_WINDOW
BUYBACK_EXPECTED_WINDOW
HOLDER_INCREASE_EXPECTED_WINDOW
HOLDER_DECREASE_EXPECTED_WINDOW
BUYBACK_WINDOW_START / BUYBACK_WINDOW_END
HOLDER_INCREASE_WINDOW_START / HOLDER_INCREASE_WINDOW_END
HOLDER_DECREASE_WINDOW_START / HOLDER_DECREASE_WINDOW_END
DIVIDEND_RECORD_DATE
DIVIDEND_EX_DATE
DIVIDEND_PAYMENT_DATE
SHAREHOLDER_MEETING
RESUMPTION
EARNINGS_RELEASE
```

`*_EXECUTION_WINDOW` 表示完整未来确认区间；`*_EXPECTED_WINDOW` 表示具有官方预期窗口证据但日期仍低于确认区间确定性的区间；`*_WINDOW_START / *_WINDOW_END` 用于只有单个明确未来边界，或确认区间已经开始后仅剩未来结束边界的情况。

没有足够证据的重大重组、股权激励或其他期限不会因为标题里出现一个日期就自动进入日历。

## 跨来源去重

同一逻辑事件可能同时出现在公司事件层和股权资本结构层。去重只在事件类型和日期一致的前提下使用以下强身份之一：

1. 相同 `source_event_id`；
2. 相同 provider + `source_document_id`；
3. 完全一致的规范化标题；
4. 最后才使用当前 `event_id`。

范围事件还保留 `date_end` 和 `date_certainty`，不会把不同范围语义强行合并。合并后保留全部 `source_relations`，并仅用非空事实补充 `details`。日期或事件类型不同的事项不会因为标题相似被误合并。

## 交易日邻近与事件重叠

每条事件通过 `config/a_share_trading_calendar.json` 获得：

```text
verification_status
is_trading_day
previous_trading_day
next_trading_day
calendar_days_from_previous_trading_day
calendar_days_to_next_trading_day
nearest_trading_day_distance_calendar_days
near_trading_day
```

日历超出覆盖范围或解析异常时，`verification_status=UNVERIFIED`，不会猜测交易日。`near_trading_day` 只在已验证日历上计算：事件当天为交易日，或距离已验证交易日不超过 1 个自然日。

对于范围事件，交易日邻近上下文与窗口分桶一样，以 `event_date`（范围起点）为锚点；当前不会自行推导整个区间内的交易日数量。

同日事件会获得 `overlap_context`，顶层摘要提供同日重叠日期数和高重要性重叠日期数。重叠只是事实密度，不表示风险方向。

## changes_since_previous 集成

`changes_since_previous.upcoming_events` 只报告有语义的变化：

- `NEW_UPCOMING_EVENT`；
- `EVENT_REMOVED_FROM_UPCOMING`；
- 有当前来源状态证据时的 `EVENT_COMPLETED / EVENT_CANCELLED`；
- `EVENT_DATE_CHANGED`（包括 `event_date` 或 `date_end` 修订）；
- `EVENT_STATUS_CHANGED`；
- `ENTERED_30D_WINDOW / ENTERED_7D_WINDOW`。

普通每天减少一天的 `days_until_event` 不产生 delta。范围事件的窗口迁移仍以范围起点 bucket 为准。只有 before/after 两侧日历都可比较时才判断移除或状态变化；缺失/DEFERRED/不可比较 baseline 会 fail closed 为不可比较，而不是制造“全部事件消失”。

## provenance / freshness / quality

`upcoming_events.metadata` 遵循统一 `provenance_freshness_quality_v1` 核心字段：

```text
source / source_type / source_tier
fetched_at / data_time / lag_seconds
freshness / freshness_policy
confidence / quality / fallback_used / quality_flags
```

日历自身是派生层，因此 `source_type/source_tier=DERIVED`，并通过 `provenance.derived_from` 保留实际事实层。来源 `PARTIAL/DEFERRED/UNAVAILABLE` 会传播到质量；交易日上下文无法验证时只降级上下文质量，不会否定已经确认的公司事件日期。

与日期语义相关的 metadata 还包括：

```text
date_certainty_contract
window_semantics
excluded_unproven_company_event_count
excluded_plan_date_semantics.invalid_confirmed_range
excluded_plan_date_semantics.expected_window_without_official_basis
excluded_plan_date_semantics.unknown_window
```

重要质量标记包括：

```text
UNPROVEN_DATES_EXCLUDED_FAIL_CLOSED
TRADING_DAY_CONTEXT_UNVERIFIED
SOURCE_<LAYER>_<STATUS>
```

顶层 `upcoming_events_summary` 额外汇总 `quality_by_code` 和 `quality_flag_counts`，供全局 `data_metadata` / LLM 消费。

## 验证入口

唯一完成验证入口为：

```bash
python3 scripts/test_upcoming_events_completion.py
```

该入口聚合核心单日/范围/expected/unknown 日期合同、标题日期语义、跨来源去重、交易日 fail-closed、同日重叠、`changes_since_previous`、provenance/freshness/quality contract 测试，并由 required `pre-merge-security-gate` 的 Safety tests 执行。

当前实现若要新增未来日期来源，必须同时满足：来源语义可证明、无猜测、去重身份稳定、changes 可比较、metadata/provenance 可追溯，并扩展上述 completion tests。
