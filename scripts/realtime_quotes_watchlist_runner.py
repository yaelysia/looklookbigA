from concurrent.futures import ThreadPoolExecutor

import alpha_provider_contract
import breadth_bootstrap
import capital_flow_changes
import capital_flow_context
import capital_flow_history_bridge
import capital_flow_margin_bridge
import capital_flow_metadata_bridge
import capital_flow_window_bridge
import changes_comparability
import changes_metadata_bridge
import changes_since_previous
import changes_summary_finalizer
import company_event_coverage
import company_event_facts
import company_event_metadata
import company_events
import config_security
import daily_k_context
import data_metadata
import data_policy
import data_policy_bridge
import event_fact_continuity
import fundamentals_cache_continuity
import fundamentals_changes
import fundamentals_context
import fundamentals_history_bridge
import fundamentals_metadata_bridge
import fundamentals_period_bridge
import fundamentals_policy_bridge
import fundamentals_quality_bridge
import fundamentals_review_hardening
import fundamentals_schema_bridge
import history_continuity
import history_store
import intraday_fast_tail
import intraday_metrics
import live_price_guard
import market_calendar
import market_breadth_source
import market_environment
import minute_history
import ownership_capital
import performance_fast_path
import quote_resilience
import realtime_quotes_watchlist as base
import relative_strength_windows
import transport_security


fundamentals_policy_bridge.install(data_policy)
data_policy_bridge.install(data_metadata)
capital_flow_metadata_bridge.install(data_metadata)
fundamentals_metadata_bridge.install(data_metadata)
capital_flow_window_bridge.install(capital_flow_context)
capital_flow_margin_bridge.install(capital_flow_context)
fundamentals_cache_continuity.install(fundamentals_context)
fundamentals_period_bridge.install(fundamentals_context)
fundamentals_quality_bridge.install(fundamentals_context)
fundamentals_schema_bridge.install(fundamentals_context)
fundamentals_review_hardening.install(fundamentals_context)
changes_metadata_bridge.install(data_metadata)
changes_comparability.install(changes_since_previous)
event_fact_continuity.install(company_events, company_event_facts)
company_event_coverage.install(company_events)
history_continuity.install_manifest_revision(history_store)
capital_flow_history_bridge.install(history_store)
fundamentals_history_bridge.install(history_store)
config_security.install(base)

EXECUTION_MODE = performance_fast_path.configure_mode(base)

transport_security.install_quote_resilience(quote_resilience)
market_environment.configure_indices(base, quote_resilience)
quote_resilience.install(base)
performance_fast_path.install_network_deadlines(base, quote_resilience, company_events)
performance_fast_path.install_concurrent_detail(base)

if performance_fast_path.is_fast():
    intraday_fast_tail.install_fast_indices(base, quote_resilience)
    market_environment.fetch_market_breadth = (
        lambda base_obj, now, indices=None: breadth_bootstrap.fetch_market_breadth(
            base_obj,
            now,
            indices,
            market_breadth_source.fetch_market_breadth,
        )
    )
else:
    market_environment.fetch_market_breadth = market_breadth_source.fetch_market_breadth
market_environment.install(base)

performance_fast_path.install_fast_daily_cache(history_store, base, daily_k_context)
intraday_metrics.install(base)
minute_history.install(base)
capital_flow_context.install(base)
live_price_guard.install(base)
daily_k_context.install(base)
performance_fast_path.install_fast_daily_metadata(data_metadata)
performance_fast_path.install_parallel_main(base)


if __name__ == "__main__":
    runtime_config = base.load_config()
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
        performance_fast_path.timed_call(
            "market_calendar", market_calendar.finalize_snapshot, base.SNAPSHOT_PATH
        )
        performance_fast_path.timed_call("intraday_metrics", intraday_metrics.finalize_snapshot, base.SNAPSHOT_PATH)
        performance_fast_path.timed_call("daily_k_context", daily_k_context.finalize_snapshot, base.SNAPSHOT_PATH)
        performance_fast_path.timed_call(
            "observation_identity", history_continuity.finalize_snapshot, base.SNAPSHOT_PATH
        )
        performance_fast_path.timed_call(
            "minute_history", minute_history.finalize_snapshot, base.SNAPSHOT_PATH
        )
        performance_fast_path.timed_call(
            "relative_strength_windows",
            relative_strength_windows.finalize_snapshot,
            base.SNAPSHOT_PATH,
            base,
            runtime_config,
        )
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
            "fundamentals",
            fundamentals_context.finalize_snapshot,
            base.SNAPSHOT_PATH,
            base,
            EXECUTION_MODE,
        )
        performance_fast_path.timed_call(
            "capital_flow",
            capital_flow_context.finalize_snapshot,
            base.SNAPSHOT_PATH,
            base,
            EXECUTION_MODE,
        )
        performance_fast_path.timed_call(
            "ownership_and_capital",
            ownership_capital.finalize_snapshot,
            base.SNAPSHOT_PATH,
            base,
            EXECUTION_MODE,
        )
        performance_fast_path.timed_call(
            "changes_since_previous", changes_since_previous.finalize_snapshot, base.SNAPSHOT_PATH
        )
        performance_fast_path.timed_call(
            "capital_flow_changes", capital_flow_changes.finalize_snapshot, base.SNAPSHOT_PATH
        )
        performance_fast_path.timed_call(
            "fundamentals_changes", fundamentals_changes.finalize_snapshot, base.SNAPSHOT_PATH
        )
        performance_fast_path.timed_call(
            "changes_summary_final", changes_summary_finalizer.finalize_snapshot, base.SNAPSHOT_PATH
        )
        performance_fast_path.timed_call("data_metadata", data_metadata.finalize_snapshot, base.SNAPSHOT_PATH)
        performance_fast_path.timed_call(
            "alpha_provider_contract", alpha_provider_contract.finalize_snapshot, base.SNAPSHOT_PATH
        )

        intraday_fast_tail.finalize_performance(base.SNAPSHOT_PATH)
        history_store.archive_final_snapshot(base.SNAPSHOT_PATH)
    finally:
        if event_pool is not None:
            event_pool.shutdown(wait=True)
