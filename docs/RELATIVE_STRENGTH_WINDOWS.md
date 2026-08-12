# 同步分钟相对强弱

`detail_stocks.<code>.relative_strength_windows` 用同一交易日、同一已终结分钟截止点计算重点股相对基准的 5 / 15 / 30 分钟收益，避免把不同时间范围的当日涨跌幅相减后误称为短周期强弱。

## 基准与公式

每个 detail stock 至少输出：

- 配置中以该股票为 `target_code` 的同业篮子；
- 创业板指 `sz399006`；
- 中证1000 `sh000852`。

窗口需要 `N + 1` 个连续分钟端点：

```text
target_return    = target_end / target_start - 1
benchmark_return = benchmark_end / benchmark_start - 1
excess_return    = target_return - benchmark_return
```

同业篮子按可验证成员收益的等权中位数聚合，同时报告均值。目标、同业和指数都使用交易日历给出的最后已终结分钟；正在形成的分钟记录在 `forming_minute_excluded`，不参与计算。

## 质量与覆盖

每个窗口保留 `window_start`、`window_end/cutoff`、目标收益、基准收益、超额收益、状态、覆盖详情和 reason codes：

- 连续且全覆盖为 `OK / PASS`；
- 同业仅部分成员连续可用为 `PARTIAL / DEGRADED`；
- 窗口内缺分钟为 `GAPPED`；
- 交易日不一致、数据源失败或分钟不足为 `UNAVAILABLE`。

缺失端点不会用更早分钟代替，跨交易日数据不会参与计算。基准分钟抓取上限为配置硬上限 50 个股票加两个指数，并发 worker 上限为 8。

## changes 语义

`changes_since_previous.detail_stocks.<code>.relative_strength_windows_change` 只在同一交易日、相同基准身份、相同请求/覆盖同业集合及相同聚合方法下比较超额收益。成员消失或恢复时仍报告覆盖率变化，但超额收益和强弱状态标为不可比，防止把样本变化伪装成市场变化。

完整目标分钟来自 `minute_history` 的当前会话规范记录；基准分钟来自腾讯 1 分钟序列。快照同时记录算法、日历版本、freshness、quality 和 provenance。
