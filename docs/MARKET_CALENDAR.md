# A 股交易日历与会话合同

`scripts/market_calendar.py` 是行情引擎唯一的 A 股交易日与会话判断入口。实时采集窗口、日 K 已完成日期和分钟线预期时间都必须使用该模块，不再各自按“工作日”猜测。

## 权威数据与覆盖范围

日历配置位于 `config/a_share_trading_calendar.json`，当前显式覆盖 `2024-01-01` 至 `2026-12-31`。休市安排来自上海证券交易所年度休市通知，配置保留发布日、原始 URL、日历版本和有效期。

超过有效期时系统返回 `UNVERIFIED` 并降级，禁止把普通周一至周五自动当作交易日。每年交易所发布下一年度安排后，应更新休市区间、`as_of`、`calendar_version` 和 `valid_through`，再运行 `scripts/test_market_calendar.py`。

## 会话语义

公开 API 包括：

- `trading_day_verification` / `is_trading_day`：交易日判定与证据；
- `previous_trading_day` / `next_trading_day`：相邻交易日；
- `session_state` / `in_market_window`：盘前、上午、午休、下午、收盘状态与采集窗口；
- `previous_completed_session`：日 K 可声称“已完成”的最近交易日；
- `expected_minute_times`：截至指定时刻已经终结的分钟标签，明确排除形成中的分钟。

常规分钟标签为 `09:30–11:30` 与 `13:00–15:00`，共 242 个。端点与普通分钟均应用配置中的 finality grace，避免把仍可能变化的分钟提前固化。

## Snapshot 合同

顶层 `market_calendar` 至少给出：

- `calendar_version`、覆盖范围和官方来源；
- `session_date`、`session_state`、前一已完成交易日；
- 交易日验证状态；
- 已完成分钟数量、最后已完成分钟和形成中分钟。

日 K 最新完成日期与该权威层矛盾时，数据质量必须降级，不能继续输出 `OK`。
