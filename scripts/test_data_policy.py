import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import data_metadata
import data_policy
import data_policy_bridge
import history_store

CST = timezone(timedelta(hours=8))
data_policy_bridge.install(data_metadata)


def test_source_trust_tiers():
    assert data_policy.source_trust("CNINFO", source_tier="OFFICIAL")["tier"] == "A"
    assert data_policy.source_trust("Eastmoney", source_tier="PRIMARY_PROVIDER")["tier"] == "B"
    assert data_policy.source_trust("Tencent", source_tier="SECONDARY_PROVIDER")["tier"] == "B"
    assert data_policy.source_trust("market-data branch", source_type="CACHE")["tier"] == "INHERITED"
    assert data_policy.source_trust("mystery-source")["tier"] == "UNKNOWN"


def test_realtime_quote_sla():
    assert data_policy.evaluate_freshness_sla("REALTIME_QUOTE", freshness="LIVE", lag_seconds=45)["status"] == "MET"
    assert data_policy.evaluate_freshness_sla("REALTIME_QUOTE", freshness="LIVE", lag_seconds=120)["status"] == "DEGRADED"
    assert data_policy.evaluate_freshness_sla("REALTIME_QUOTE", freshness="STALE", lag_seconds=181)["status"] == "VIOLATED"
    assert data_policy.evaluate_freshness_sla("REALTIME_QUOTE", freshness="CURRENT_SESSION", lag_seconds=900)["status"] == "NOT_APPLICABLE"


def test_minute_metadata_preserves_observed_lag():
    def build(lag, freshness):
        return {
            "count": 10,
            "source": "Tencent",
            "date": "20260808",
            "last_time": "1000",
            "market_time_cst": "2026-08-08 10:00:00",
            "lag_seconds": lag,
            "freshness": freshness,
        }

    fetched = "2026-08-08T10:00:45+08:00"
    met = data_metadata._minutes_metadata(build(45, "LIVE"), fetched)
    degraded = data_metadata._minutes_metadata(build(150, "LIVE"), fetched)
    violated = data_metadata._minutes_metadata(build(181, "STALE"), fetched)
    assert met["lag_seconds"] == 45 and met["freshness_sla"]["status"] == "MET"
    assert degraded["lag_seconds"] == 150 and degraded["freshness_sla"]["status"] == "DEGRADED"
    assert violated["lag_seconds"] == 181 and violated["freshness_sla"]["status"] == "VIOLATED"


def test_event_discovery_requires_first_seen():
    published = datetime(2026, 8, 8, 10, 0, tzinfo=CST)
    missing = data_policy.evaluate_freshness_sla("COMPANY_EVENT", data_time=published.isoformat())
    met = data_policy.evaluate_freshness_sla(
        "COMPANY_EVENT", data_time=published.isoformat(),
        first_seen_at=(published + timedelta(seconds=240)).isoformat(),
    )
    assert missing["status"] == "UNMEASURED"
    assert met["status"] == "MET"


def test_session_completeness_requires_verified_evidence():
    common = {
        "data_time": "2026-08-07",
        "fetched_at": "2026-08-08T10:00:00+08:00",
        "freshness": "LATEST_COMPLETED_BAR",
    }
    met = data_policy.evaluate_freshness_sla(
        "DAILY_K", session_verified=True, completed_session_age=1,
        session_validation_state="INCREMENTAL_REFRESH", **common,
    )
    unmeasured = data_policy.evaluate_freshness_sla(
        "DAILY_K", session_verified=False, completed_session_age=None,
        session_validation_state="STALE_FALLBACK", **common,
    )
    violated = data_policy.evaluate_freshness_sla(
        "DAILY_K", session_verified=True, completed_session_age=2,
        session_validation_state="INCREMENTAL_REFRESH", **common,
    )
    assert met["status"] == "MET"
    assert unmeasured["status"] == "UNMEASURED"
    assert unmeasured["reason"] == "SESSION_COMPLETENESS_UNVERIFIED"
    assert violated["status"] == "VIOLATED"
    assert violated["reason"] == "COMPLETED_SESSION_TOO_OLD"


def test_daily_metadata_rejects_stale_fallback_as_latest():
    fetched = "2026-08-08T10:00:00+08:00"
    valid = {
        "status": "OK", "source": "History cache + Tencent qfq", "errors": [],
        "latest_completed_date": "2026-08-07", "previous_day": {"date": "2026-08-07"},
        "current_partial_bar": {"date": "2026-08-08"},
        "cache": {"state": "INCREMENTAL_REFRESH", "validation_key": "2026-08-08:intraday"},
    }
    stale = {
        "status": "OK", "source": "History stale cache (Tencent qfq)",
        "errors": ["history validation failed"],
        "latest_completed_date": "2026-07-31", "previous_day": {"date": "2026-07-31"},
        "current_partial_bar": None,
        "cache": {"state": "STALE_FALLBACK", "validation_key": "2026-08-08:intraday"},
    }
    good_meta = data_metadata._daily_metadata(valid, fetched)
    stale_meta = data_metadata._daily_metadata(stale, fetched)
    assert good_meta["freshness"] == "LATEST_COMPLETED_BAR"
    assert good_meta["freshness_sla"]["status"] == "MET"
    assert good_meta["freshness_sla"]["observed_completed_session_age"] == 1
    assert stale_meta["quality"] == "DEGRADED"
    assert stale_meta["freshness"] == "COMPLETED_BAR_UNVERIFIED"
    assert stale_meta["freshness_sla"]["status"] == "UNMEASURED"
    assert stale_meta["freshness_sla"]["reason"] == "SESSION_COMPLETENESS_UNVERIFIED"


def test_stale_daily_cache_cannot_launder_into_same_phase_hit():
    fixed_now = datetime(2026, 8, 7, 10, 0, tzinfo=CST)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)

    bars = []
    start = datetime(2026, 5, 1)
    for idx in range(60):
        bars.append({"date": (start + timedelta(days=idx)).strftime("%Y-%m-%d")})

    calls = {"count": 0}

    def failing_fetch(base_obj, code, limit=90):
        calls["count"] += 1
        raise RuntimeError("synthetic daily-K validation failure")

    base = SimpleNamespace(CST=CST)
    daily = SimpleNamespace(fetch_daily_bars=failing_fetch)
    old_datetime = history_store.datetime
    old_history_dir = os.environ.get("MARKET_HISTORY_DIR")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.environ["MARKET_HISTORY_DIR"] = tmp
            history_store.datetime = FixedDateTime
            history_store.CACHE_META.clear()
            history_store._save_cache(
                "002558",
                "Tencent qfq",
                bars,
                "2026-08-07:preopen",
                "INCREMENTAL_VALIDATION",
                fixed_now,
                [],
            )
            history_store.install_daily_k_cache(base, daily)

            # Run N: validation fails and persists the current key as stale.
            daily.fetch_daily_bars(base, "002558")
            first = dict(history_store.CACHE_META["002558"])
            first_context = {
                "status": "OK",
                "source": "History stale cache (Tencent qfq)",
                "errors": ["synthetic daily-K validation failure"],
                "latest_completed_date": first["latest_bar_date"],
                "previous_day": {"date": first["latest_bar_date"]},
                "current_partial_bar": None,
                "cache": first,
            }
            first_meta = data_metadata._daily_metadata(first_context, fixed_now.isoformat())
            assert first["state"] == "STALE_FALLBACK"
            assert first["validation_mode"] == "STALE_CACHE_FALLBACK"
            assert first_meta["freshness_sla"]["status"] == "UNMEASURED"
            assert calls["count"] == 1

            # Run N+1 in the exact same validation phase. The persisted stale
            # mode must not qualify for the zero-network HIT fast path.
            daily.fetch_daily_bars(base, "002558")
            second = dict(history_store.CACHE_META["002558"])
            persisted = history_store._load_json(history_store._cache_path("002558"))
            second_context = dict(first_context)
            second_context["cache"] = second
            second_meta = data_metadata._daily_metadata(second_context, fixed_now.isoformat())
            assert second["state"] == "STALE_FALLBACK"
            assert second["state"] != "HIT"
            assert second["validation_mode"] == "STALE_CACHE_FALLBACK"
            assert persisted["validation_mode"] == "STALE_CACHE_FALLBACK"
            assert second_meta["freshness_sla"]["status"] == "UNMEASURED"
            assert calls["count"] == 2
        finally:
            history_store.datetime = old_datetime
            history_store.CACHE_META.clear()
            if old_history_dir is None:
                os.environ.pop("MARKET_HISTORY_DIR", None)
            else:
                os.environ["MARKET_HISTORY_DIR"] = old_history_dir


def test_sla_total_counts_are_not_truncated():
    nodes = []
    for idx in range(83):
        nodes.append({"metadata": data_metadata._metadata(
            f"unknown-{idx}", "2026-08-08T10:00:00+08:00",
            freshness="UNKNOWN", freshness_policy="UNREGISTERED_POLICY", quality="PASS",
        )})
    snapshot = {"detail_stocks": {}, "light_stocks": {}, "indices": {}, "groups": {}, "nodes": nodes}
    quality = data_metadata._quality_summary(snapshot)
    compact = data_metadata._llm_summary(snapshot, quality)
    assert quality["freshness_sla_unmeasured_count"] == 83
    assert len(quality["freshness_sla_unmeasured"]) == 50
    assert compact["freshness_sla_unmeasured_count"] == 83


def test_future_classes_and_manifest():
    required = {"NEWS", "RESEARCH_REPORT", "DRAGON_TIGER_LIST", "REGULATORY_EVENT", "INTRADAY_FUND_FLOW", "DAILY_FINANCING", "MACRO_RELEASE", "INDUSTRY_EVENT"}
    assert required.issubset(data_policy.FRESHNESS_POLICIES)
    snapshot = {"runner_time_utc": "2026-08-08T02:00:00Z", "detail_stocks": {}, "light_stocks": {}, "indices": {}, "groups": {}}
    data_metadata.decorate_snapshot(snapshot)
    assert snapshot["schema_version"] >= 13
    assert snapshot["features"]["data_policy"] == "v1"
    assert snapshot["data_policy"]["current_capabilities"]["continuous_watcher"] == "NOT_IMPLEMENTED"


def main():
    tests = [
        test_source_trust_tiers,
        test_realtime_quote_sla,
        test_minute_metadata_preserves_observed_lag,
        test_event_discovery_requires_first_seen,
        test_session_completeness_requires_verified_evidence,
        test_daily_metadata_rejects_stale_fallback_as_latest,
        test_stale_daily_cache_cannot_launder_into_same_phase_hit,
        test_sla_total_counts_are_not_truncated,
        test_future_classes_and_manifest,
    ]
    for test in tests:
        test()
    print(f"DATA_POLICY_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
