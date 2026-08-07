import daily_k_context
import history_store
import intraday_metrics
import live_price_guard
import quote_resilience
import realtime_quotes_watchlist as base


# Install the quote-source resilience layer before downstream enrichers.
# Current quotes/indices can fall back from Eastmoney to Tencent, while the
# live-price guard still owns the final rule that stale/history data can never
# become a usable current price.
quote_resilience.install(base)
history_store.install_daily_k_cache(base, daily_k_context)
intraday_metrics.install(base)
live_price_guard.install(base)
daily_k_context.install(base)


if __name__ == "__main__":
    base.main()
    intraday_metrics.finalize_snapshot(base.SNAPSHOT_PATH)
    daily_k_context.finalize_snapshot(base.SNAPSHOT_PATH)
    history_store.finalize_snapshot(base.SNAPSHOT_PATH)
    live_price_guard.finalize_snapshot(base.SNAPSHOT_PATH)
    quote_resilience.finalize_snapshot(base.SNAPSHOT_PATH)
