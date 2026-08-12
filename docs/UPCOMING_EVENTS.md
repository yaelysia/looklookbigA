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
- `detail_stocks.<code>.ownership_and_capital.buyback_and_holder_plans` 中显式披露的回购、增持、减持执行窗口边界。

FAST/FULL 都只消费当前快照里已经形成的这些事实。`upcoming_events` 本身不会为了补日历再请求外部 provider。

## 日期语义与 fail-closed 规则

事件对象保留 `date_certainty`。当前 producer 只在能证明单个精确日期时输出：

```text
CONFIRMED_DATE
```

`CONFIRMED_RANGE / EXPECTED_WINDOW / UNKNOWN` 属于上层事件日历合同需要区分的语义，但当前实现不会为了填满枚举而伪造它们：没有官方可验证的范围或预期窗口 producer 时，事件不会被提升成这些状态。后续如果接入明确的官方范围/预期窗口，必须同时提供 `date_end`、降低适用 confidence，并补充 completion tests。

当前精确日期的允许来源包括：

- 解禁：结构化事实里的 `unlock_date`，且与事件日期语义一致；
- 回购/增减持：明确的 `window_start_date / window_end_date`，当前以独立边界事件输出；
- 分红：公告标题明确标注的股权登记日、除权除息日、现金红利发放日；
- 股东大会：标题中明确的召开日期/时间语义；
- 复牌：标题中明确的复牌日/日期/时间语义；
- 定期报告、业绩快报、业绩预告：只有标题明确标注披露日/预约披露日时才输出 `EARNINGS_RELEASE`。

普通公告 `effective_date`、标题中没有业务标签的日期、历史财报发布时间和其他不能证明未来业务语义的日期全部 fail closed。被排除的可疑日期通过 metadata / quality flag 留痕，而不是被静默猜测。

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

`calendar_summary.next_30d_event_count` 等累计指标会跨桶统计到对应阈值，因此不要把累计指标与单桶数组长度直接等同。

## 事件对象

当前事件对象的稳定核心为：

```json
{
  "event_id": "upcoming:<source-event>:<type>:<date>",
  "event_type": "UNLOCK",
  "title": "...",
  "event_date": "2026-08-20",
  "date_end": null,
  "date_certainty": "CONFIRMED_DATE",
  "days_until_event": 7,
  "importance": "HIGH",
  "status": "UPCOMING",
  "source_event_id": "...",
  "source_relations": [],
  "details": {},
  "trading_day_context": {},
  "overlap_context": {}
}
```

`event_id` 便于快照内引用，但跨快照变化比较优先使用稳定 `source_event_id + event_type`。这是因为日期修订会改变包含日期的 `event_id`；比较层会把这种情况识别为 `EVENT_DATE_CHANGED`，而不是伪造一条删除和一条新增。

## 事件类型覆盖

当前明确支持的高价值日期语义包括：

```text
UNLOCK
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

没有足够证据的重大重组、股权激励或其他期限不会因为标题里出现一个日期就自动进入日历。

## 跨来源去重

同一逻辑事件可能同时出现在公司事件层和股权资本结构层。去重只在事件类型和日期一致的前提下使用以下强身份之一：

1. 相同 `source_event_id`；
2. 相同 provider + `source_document_id`；
3. 完全一致的规范化标题；
4. 最后才使用当前 `event_id`。

合并后保留全部 `source_relations`，并仅用非空事实补充 `details`。日期或事件类型不同的事项不会因为标题相似被误合并。

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

同日事件会获得 `overlap_context`，顶层摘要提供同日重叠日期数和高重要性重叠日期数。重叠只是事实密度，不表示风险方向。

## changes_since_previous 集成

`changes_since_previous.upcoming_events` 只报告有语义的变化：

- `NEW_UPCOMING_EVENT`；
- `EVENT_REMOVED_FROM_UPCOMING`；
- 有当前来源状态证据时的 `EVENT_COMPLETED / EVENT_CANCELLED`；
- `EVENT_DATE_CHANGED`；
- `EVENT_STATUS_CHANGED`；
- `ENTERED_30D_WINDOW / ENTERED_7D_WINDOW`。

普通每天减少一天的 `days_until_event` 不产生 delta。只有 before/after 两侧日历都可比较时才判断移除或状态变化；缺失/DEFERRED/不可比较 baseline 会 fail closed 为不可比较，而不是制造“全部事件消失”。

## provenance / freshness / quality

`upcoming_events.metadata` 遵循统一 `provenance_freshness_quality_v1` 核心字段：

```text
source / source_type / source_tier
fetched_at / data_time / lag_seconds
freshness / freshness_policy
confidence / quality / fallback_used / quality_flags
```

日历自身是派生层，因此 `source_type/source_tier=DERIVED`，并通过 `provenance.derived_from` 保留实际事实层。来源 `PARTIAL/DEFERRED/UNAVAILABLE` 会传播到质量；交易日上下文无法验证时只降级上下文质量，不会否定已经确认的公司事件日期。

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

该入口聚合核心日期、标题日期语义、跨来源去重、交易日 fail-closed、同日重叠、`changes_since_previous`、provenance/freshness/quality contract 测试，并由 required `pre-merge-security-gate` 的 Safety tests 执行。

当前实现若要新增未来日期来源，必须同时满足：来源语义可证明、无猜测、去重身份稳定、changes 可比较、metadata/provenance 可追溯，并扩展上述 completion tests。
