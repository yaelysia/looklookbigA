from concurrent.futures import ThreadPoolExecutor

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
import intraday_fast_tail
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
config_security.install(base)

# AUTO resolves to INTRADAY_FAST only while the existing market-window guard is
# active. Explicit FULL/INTRADAY_FAST from the workflow remains supported.
EXECUTION_MODE = performance_fast_path.configure_mode(base)

# Never downgrade a failed HTTPS market-data request to plaintext HTTP.
transport_security.install_quote_resilience(quote_resilience)

# Extend the broad-market index set before the quote fetcher is installed.
market_environment.configure_indices(base, quote_resilience)
quote_resilience.install(base)

# FAST bounds request tail latency. FULL retains the original provider budgets.
performance_fast_path.install_network_deadlines(base, quote_resilience, company_events)
performance_fast_path.install_concurrent_detail(base)

if performance_fast_path.is_fast():
    # The six-index dual-source consensus is valuable in FULL, but waiting for
    # every primary request produced >10s tail latency in real intraday runs.
    # FAST uses one Tencent batch (still Trust B) and marks it as single-source.
    intraday_fast_tail.install_fast_indices(base, quote_resilience)
    # Market breadth is explicitly non-critical in FAST. Never issue breadth
    # network I/O on the decision path; use only <=10m same-session cache.
    market_environment.fetch_market_breadth = (
        lambda base_obj, now, indices=None: intraday_fast_tail.cache_only_market_breadth(
            base_obj, now, indices
        )
    )
else:
    market_environment.fetch_market_breadth = market_breadth_source.fetch_market_breadth
market_environment.install(base)

# FAST reuses daily-K cache without blocking on validation. Only today's
# successful validation may be HIT; older/failed state stays explicitly
# DEGRADED + UNMEASURED through the policy layer.
performance_fast_path.install_fast_daily_cache(history_store, base, daily_k_context)
intraday_metrics.install(base)
live_price_guard.install(base)
daily_k_context.install(base)
performance_fast_path.install_fast_daily_metadata(data_metadata)

# Detail stocks, light peers and indices are independent collection groups.
performance_fast_path.install_parallel_main(base)


if __name__ == "__main__":
    runtime_config = base.load_config()

    # Event discovery remains live in FAST, but it starts before market
    # collection so the two network workloads overlap instead of adding their
    # latencies. PDF fact extraction remains deferred to FULL.
    event_pool = None
    event_future = None
    if performance_fast_path.is_fast():
        event_pool = ThreadPoolExecutor(max_workers=1)
        event_future = event_pool.submit(
            intraday_fast_tail.prefetch_company_events,
            runtime_config,
            company_events,
        )

    try:
        performance_fast_path.timed_call("base_collection", base.main)
        performance_fast_path.timed_call("intraday_metrics", intraday_metrics.finalize_snapshot, base.SNAPSHOT_PATH)
        performance_fast_path.timed_call("daily_k_context", daily_k_context.finalize_snapshot, base.SNAPSHOT_PATH)

        # Prepare the previous-snapshot pointer, but do not advance the manifest
        # until the current run has all enrichment layers.
        performance_fast_path.timed_call("history_prepare", history_store.finalize_snapshot, base.SNAPSHOT_PATH)
        performance_fast_path.timed_call("live_price_guard", live_price_guard.finalize_snapshot, base.SNAPSHOT_PATH)
        performance_fast_path.timed_call("quote_resilience", quote_resilience.finalize_snapshot, base.SNAPSHOT_PATH)
        performance_fast_path.timed_call("market_environment", market_environment.finalize_snapshot, base.SNAPSHOT_PATH)

        if performance_fast_path.is_fast():
            prefetched = event_future.result()
            performance_fast_path.timed_call(
                "company_events_apply",
                intraday_fast_tail.apply_prefetched_company_events,
                base.SNAPSHOT_PATH,
                prefetched,
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

        # Compare against the last fully archived snapshot. This stays before
        # metadata so the changes node gets the shared contract.
        performance_fast_path.timed_call(
            "changes_since_previous", changes_since_previous.finalize_snapshot, base.SNAPSHOT_PATH
        )
        performance_fast_path.timed_call("data_metadata", data_metadata.finalize_snapshot, base.SNAPSHOT_PATH)

        # Record decision latency before local archive bookkeeping. The separate
        # persist-history workflow job is not on the read/decision path.
        performance_fast_path.finalize_performance(base.SNAPSHOT_PATH)

        # Archive first, then advance the manifest atomically.
        history_store.archive_final_snapshot(base.SNAPSHOT_PATH)
    finally:
        if event_pool is not None:
            event_pool.shutdown(wait=True)
