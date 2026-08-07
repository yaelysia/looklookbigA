import changes_since_previous
import company_event_facts
import company_event_metadata
import company_events
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


# Treat the watchlist as untrusted input before any network work starts.
config_security.install(base)

# Never downgrade a failed HTTPS market-data request to plaintext HTTP.
transport_security.install_quote_resilience(quote_resilience)

# Extend the broad-market index set before resilient index fetching is installed.
market_environment.configure_indices(base, quote_resilience)
quote_resilience.install(base)
market_environment.fetch_market_breadth = market_breadth_source.fetch_market_breadth
market_environment.install(base)
history_store.install_daily_k_cache(base, daily_k_context)
intraday_metrics.install(base)
live_price_guard.install(base)
daily_k_context.install(base)


if __name__ == "__main__":
    runtime_config = base.load_config()
    base.main()
    intraday_metrics.finalize_snapshot(base.SNAPSHOT_PATH)
    daily_k_context.finalize_snapshot(base.SNAPSHOT_PATH)

    # Read the previous fully archived snapshot pointer, but do not advance it
    # until every current-run enrichment layer is complete.
    history_store.finalize_snapshot(base.SNAPSHOT_PATH)
    live_price_guard.finalize_snapshot(base.SNAPSHOT_PATH)
    quote_resilience.finalize_snapshot(base.SNAPSHOT_PATH)
    market_environment.finalize_snapshot(base.SNAPSHOT_PATH)

    # Official company events are collected before delta analysis so stable
    # event IDs participate in new/updated/closed changes_since_previous.
    company_events.finalize_snapshot(base.SNAPSHOT_PATH, runtime_config)
    # Important structured event types may use their official CNINFO PDF for
    # deterministic fact extraction. Failure only degrades fact enrichment;
    # the official event record itself remains available.
    company_event_facts.finalize_snapshot(base.SNAPSHOT_PATH)
    company_event_metadata.finalize_snapshot(base.SNAPSHOT_PATH)
    changes_since_previous.finalize_snapshot(base.SNAPSHOT_PATH)
    data_metadata.finalize_snapshot(base.SNAPSHOT_PATH)

    # Current snapshot becomes the next baseline only after all layers exist.
    history_store.archive_final_snapshot(base.SNAPSHOT_PATH)
