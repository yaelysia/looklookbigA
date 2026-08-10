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


data_policy_bridge.install(data_metadata)
changes_metadata_bridge.install(data_metadata)
changes_comparability.install(changes_since_previous)
company_event_coverage.install(company_events)
config_security.install(base)

EXECUTION_MODE = performance_fast_path.configure_mode(base)

transport_security.install_quote_resilience(quote_resilience)
market_environment.configure_indices(base, quote_resilience)
quote_resilience.install(base)
performance_fast_path.install_network_deadlines(base, quote_resilience, company_events)
performance_fast_path.install_concurrent_detail(base)

if performance_fast_path.is_fast():
    # FAST indices are a single Tencent Trust-B batch. This avoids the long tail
    # from waiting for six primary Eastmoney requests solely for consensus.
    intraday_fast_tail.install_fast_indices(base, quote_resilience)
    # Breadth is non-critical: no breadth network I/O is allowed on the FAST
    # decision path. Only same-session cache <=10m may be reused.
    market_environment.fetch_market_breadth = (
        lambda base_obj, now, indices=None: intraday_fast_tail.cache_only_market_breadth(
            base_obj, now, indices
        )
    )
else:
    market_environment.fetch_market_breadth = market_breadth_source.fetch_market_breadth
market_environment.install(base)

performance_fast_path.install_fast_daily_cache(history_store, base, daily_k_context)
intraday_metrics.install(base)
live_price_guard.install(base)
daily_k_context.install(base)
performance_fast_path.install_fast_daily_metadata(data_metadata)
performance_fast_path.install_parallel_main(base)


if __name__ == "__main__":
    runtime_config = base.load_config()

    # Start official event discovery concurrently with market collection. Event
    # freshness is preserved; only its waiting time is overlapped. PDF fact
    # extraction remains the deferred secondary layer in FAST.
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

        performance_fast_path.timed_call(
            "changes_since_previous", changes_since_previous.finalize_snapshot, base.SNAPSHOT_PATH
        )
        performance_fast_path.timed_call("data_metadata", data_metadata.finalize_snapshot, base.SNAPSHOT_PATH)

        # Measure user-visible decision readiness before local archive and the
        # separate persist-history job.
        intraday_fast_tail.finalize_performance(base.SNAPSHOT_PATH)
        history_store.archive_final_snapshot(base.SNAPSHOT_PATH)
    finally:
        if event_pool is not None:
            event_pool.shutdown(wait=True)
