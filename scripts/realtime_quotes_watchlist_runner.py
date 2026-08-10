import changes_comparability
import changes_metadata_bridge
import changes_since_previous
import company_event_coverage
import company_event_facts
import company_event_metadata
import company_events
import config_security
import daily_k_context
import data_metadata
import data_policy_bridge
import history_store
import intraday_metrics
import live_price_guard
import market_breadth_source
import market_environment
import performance_fast_path
import quote_resilience
import realtime_quotes_watchlist as base
import transport_security


# Attach source-authority and freshness-SLA policy to the shared metadata
# contract before any downstream metadata adapters use data_metadata._metadata.
data_policy_bridge.install(data_metadata)
# Keep #34 integration additive: extend the already-reviewed #35 metadata layer
# instead of carrying an older copy of data_metadata.py on this stacked branch.
changes_metadata_bridge.install(data_metadata)
# Tighten delta comparability without modifying the already-reviewed #37
# provenance implementation. Cumulative turnover and peer-relative metrics are
# only allowed to produce deltas when their comparison universes are confirmed.
changes_comparability.install(changes_since_previous)
# Historical announcement coverage must be complete before the event cache is
# allowed to switch to ordinary recent-overlap incremental refresh.
company_event_coverage.install(company_events)

# Treat the watchlist as untrusted input before any network work starts.
# This applies equally to the repository default config and reusable-workflow
# caller/inline configs.
config_security.install(base)

# AUTO resolves to INTRADAY_FAST only while the existing market-window guard is
# active. Explicit FULL/INTRADAY_FAST from the workflow remains supported.
EXECUTION_MODE = performance_fast_path.configure_mode(base)

# Never downgrade a failed HTTPS market-data request to plaintext HTTP.
transport_security.install_quote_resilience(quote_resilience)

# Extend the broad-market index set before the resilient index fetcher is
# installed so Eastmoney + Tencent fallback applies to the extra references.
market_environment.configure_indices(base, quote_resilience)

# Current quotes/indices can fall back from Eastmoney to Tencent, while the
# live-price guard still owns the final rule that stale/history data can never
# become a usable current price.
quote_resilience.install(base)

# FAST mode bounds tail latency rather than waiting on a failing provider for
# the full generic timeout/retry budget. Provider fallback and freshness guards
# remain authoritative; this only changes how long a request may block.
performance_fast_path.install_network_deadlines(base, quote_resilience, company_events)
# Quote and minute requests for one detail stock are independent and can run in
# parallel before the daily-context wrapper is attached.
performance_fast_path.install_concurrent_detail(base)

# FULL keeps the complete breadth fallback strategy. FAST gives the full-market
# endpoint a 2s budget, then uses only a <=10m same-session cached breadth; it
# never extrapolates from an incomplete sample merely to meet latency.
if performance_fast_path.is_fast():
    market_environment.fetch_market_breadth = (
        lambda base_obj, now, indices=None: performance_fast_path.fast_market_breadth(
            base_obj, now, indices, market_breadth_source
        )
    )
else:
    market_environment.fetch_market_breadth = market_breadth_source.fetch_market_breadth
market_environment.install(base)

# FAST reuses today's already-validated daily-K cache without a new network
# validation. Older/unverified cache can still provide context, but the policy
# layer explicitly marks it DEGRADED + UNMEASURED rather than pretending it is
# the latest verified completed session.
performance_fast_path.install_fast_daily_cache(history_store, base, daily_k_context)
intraday_metrics.install(base)
live_price_guard.install(base)
daily_k_context.install(base)
performance_fast_path.install_fast_daily_metadata(data_metadata)

# Detail stocks, light peers and indices are independent collection groups.
# This replaces the old serial detail -> light -> indices critical path while
# retaining the same snapshot schema and downstream finalizers.
performance_fast_path.install_parallel_main(base)


if __name__ == "__main__":
    runtime_config = base.load_config()
    performance_fast_path.timed_call("base_collection", base.main)
    performance_fast_path.timed_call("intraday_metrics", intraday_metrics.finalize_snapshot, base.SNAPSHOT_PATH)
    performance_fast_path.timed_call("daily_k_context", daily_k_context.finalize_snapshot, base.SNAPSHOT_PATH)

    # Prepare the previous-snapshot pointer, but do not advance the manifest
    # until the current run has all enrichment layers.
    performance_fast_path.timed_call("history_prepare", history_store.finalize_snapshot, base.SNAPSHOT_PATH)
    performance_fast_path.timed_call("live_price_guard", live_price_guard.finalize_snapshot, base.SNAPSHOT_PATH)
    performance_fast_path.timed_call("quote_resilience", quote_resilience.finalize_snapshot, base.SNAPSHOT_PATH)
    performance_fast_path.timed_call("market_environment", market_environment.finalize_snapshot, base.SNAPSHOT_PATH)

    # Official event discovery remains on the FAST path because a new company
    # disclosure can matter immediately. FAST only parallelizes detail-stock
    # queries and shortens their request deadline; PDF fact enrichment is the
    # expensive secondary layer and is deferred to FULL.
    if performance_fast_path.is_fast():
        performance_fast_path.timed_call(
            "company_events",
            performance_fast_path.finalize_company_events_fast,
            base.SNAPSHOT_PATH,
            runtime_config,
            company_events,
        )
        print("COMPANY_EVENT_FACTS status=DEFERRED execution_mode=INTRADAY_FAST", flush=True)
    else:
        performance_fast_path.timed_call(
            "company_events", company_events.finalize_snapshot, base.SNAPSHOT_PATH, runtime_config
        )
        performance_fast_path.timed_call(
            "company_event_facts", company_event_facts.finalize_snapshot, base.SNAPSHOT_PATH
        )
    performance_fast_path.timed_call(
        "company_event_metadata", company_event_metadata.finalize_snapshot, base.SNAPSHOT_PATH
    )

    # Compare against the last fully archived snapshot. This stays before the
    # metadata pass so the new changes node receives the same provenance and
    # quality contract as the rest of snapshot.json.
    performance_fast_path.timed_call(
        "changes_since_previous", changes_since_previous.finalize_snapshot, base.SNAPSHOT_PATH
    )
    performance_fast_path.timed_call("data_metadata", data_metadata.finalize_snapshot, base.SNAPSHOT_PATH)

    # Record decision-critical latency before local archive bookkeeping. The
    # separate workflow persist-history job is intentionally not on this path.
    performance_fast_path.finalize_performance(base.SNAPSHOT_PATH)

    # Only now is the current snapshot complete enough to become the next
    # comparison baseline. Archive first, then advance manifest atomically.
    history_store.archive_final_snapshot(base.SNAPSHOT_PATH)
