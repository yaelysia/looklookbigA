import changes_comparability
import changes_metadata_bridge
import changes_since_previous
import config_security
import daily_k_context
import data_metadata
import history_store
import intraday_metrics
import live_price_guard
import market_breadth_source
import market_environment
import quote_resilience
import realtime_quotes_watchlist as base
import transport_security


# Keep #34 integration additive: extend the already-reviewed #35 metadata layer
# instead of carrying an older copy of data_metadata.py on this stacked branch.
changes_metadata_bridge.install(data_metadata)
# Tighten delta comparability without modifying the already-reviewed #37
# provenance implementation. Cumulative turnover and peer-relative metrics are
# only allowed to produce deltas when their comparison universes are confirmed.
changes_comparability.install(changes_since_previous)

# Treat the watchlist as untrusted input before any network work starts.
# This applies equally to the repository default config and reusable-workflow
# caller/inline configs.
config_security.install(base)

# Never downgrade a failed HTTPS market-data request to plaintext HTTP.
transport_security.install_quote_resilience(quote_resilience)

# Extend the broad-market index set before the resilient index fetcher is
# installed so Eastmoney + Tencent fallback applies to the extra references.
market_environment.configure_indices(base, quote_resilience)

# Install the quote-source resilience layer before downstream enrichers.
# Current quotes/indices can fall back from Eastmoney to Tencent, while the
# live-price guard still owns the final rule that stale/history data can never
# become a usable current price.
quote_resilience.install(base)
# Keep collection and interpretation separate. The source refuses to turn a
# truncated, gainers-sorted sample into fake market breadth.
market_environment.fetch_market_breadth = market_breadth_source.fetch_market_breadth
# Market breadth wraps the already-resilient index fetch. The breadth request
# is diagnostic: failure degrades market_environment but does not break quote
# generation or current-price safety.
market_environment.install(base)
history_store.install_daily_k_cache(base, daily_k_context)
intraday_metrics.install(base)
live_price_guard.install(base)
daily_k_context.install(base)


if __name__ == "__main__":
    base.main()
    intraday_metrics.finalize_snapshot(base.SNAPSHOT_PATH)
    daily_k_context.finalize_snapshot(base.SNAPSHOT_PATH)

    # Prepare the previous-snapshot pointer, but do not advance the manifest
    # until the current run has all enrichment layers.
    history_store.finalize_snapshot(base.SNAPSHOT_PATH)
    live_price_guard.finalize_snapshot(base.SNAPSHOT_PATH)
    quote_resilience.finalize_snapshot(base.SNAPSHOT_PATH)
    market_environment.finalize_snapshot(base.SNAPSHOT_PATH)

    # Compare against the last fully archived snapshot. This stays before the
    # metadata pass so the new changes node receives the same provenance and
    # quality contract as the rest of snapshot.json.
    changes_since_previous.finalize_snapshot(base.SNAPSHOT_PATH)
    data_metadata.finalize_snapshot(base.SNAPSHOT_PATH)

    # Only now is the current snapshot complete enough to become the next
    # comparison baseline. Archive first, then advance manifest atomically.
    history_store.archive_final_snapshot(base.SNAPSHOT_PATH)
