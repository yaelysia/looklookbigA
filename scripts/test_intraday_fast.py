import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import data_metadata
import data_policy_bridge
import history_store
import intraday_fast_tail
import performance_fast_path
import realtime_quotes_watchlist as base


def _bars(end_date, count=60):
    start = end_date - timedelta(days=count - 1)
    return [
        {
            "date": (start + timedelta(days=i)).strftime("%Y-%m-%d"),
            "open": 10.0,
            "close": 10.0,
            "high": 10.1,
            "low": 9.9,
            "volume": 100.0,
        }
        for i in range(count)
    ]


def test_auto_mode_uses_market_window():
    live = datetime(2026, 8, 10, 10, 0, tzinfo=base.CST)
    closed = datetime(2026, 8, 10, 18, 0, tzinfo=base.CST)
    assert performance_fast_path.resolve_mode(base, requested="AUTO", now=live) == "INTRADAY_FAST"
    assert performance_fast_path.resolve_mode(base, requested="AUTO", now=closed) == "FULL"
    assert performance_fast_path.resolve_mode(base, requested="FAST", now=closed) == "INTRADAY_FAST"
    assert performance_fast_path.resolve_mode(base, requested="FULL", now=live) == "FULL"


def test_fast_daily_cache_reuses_only_with_honest_state():
    os.environ["LOOKLOOK_EXECUTION_MODE"] = "INTRADAY_FAST"
    now = datetime.now(base.CST)
    calls = {"count": 0}

    def network_fetch(_base, _code, limit=90):
        calls["count"] += 1
        raise AssertionError("warm FAST cache must not hit daily-K network")

    with tempfile.TemporaryDirectory() as tmp:
        old_root = os.environ.get("MARKET_HISTORY_DIR")
        os.environ["MARKET_HISTORY_DIR"] = tmp
        try:
            bars = _bars(now - timedelta(days=1))
            history_store._save_cache(
                "002558", "Tencent qfq", bars,
                history_store._validation_key(now), "INCREMENTAL_VALIDATION", now, [],
            )
            history_store.CACHE_META.clear()
            daily = SimpleNamespace(fetch_daily_bars=network_fetch)
            performance_fast_path.install_fast_daily_cache(history_store, base, daily)
            source, got, errors = daily.fetch_daily_bars(base, "002558")
            meta = history_store.CACHE_META["002558"]
            assert len(got) == 60 and errors == []
            assert source.startswith("History cache")
            assert meta["state"] == "HIT" and meta["fast_reuse_unverified"] is False
            assert calls["count"] == 0

            history_store._save_cache(
                "002558", "Tencent qfq", bars,
                history_store._validation_key(now), "STALE_CACHE_FALLBACK", now,
                ["synthetic stale validation"],
            )
            history_store.CACHE_META.clear()
            daily2 = SimpleNamespace(fetch_daily_bars=network_fetch)
            performance_fast_path.install_fast_daily_cache(history_store, base, daily2)
            source2, got2, errors2 = daily2.fetch_daily_bars(base, "002558")
            meta2 = history_store.CACHE_META["002558"]
            assert len(got2) == 60 and errors2 == []
            assert source2.startswith("History fast cache")
            assert meta2["state"] == "FAST_REUSE_UNVERIFIED"
            assert meta2["fast_reuse_unverified"] is True
            assert calls["count"] == 0
        finally:
            history_store.CACHE_META.clear()
            if old_root is None:
                os.environ.pop("MARKET_HISTORY_DIR", None)
            else:
                os.environ["MARKET_HISTORY_DIR"] = old_root


def test_fast_unverified_daily_metadata_is_degraded_and_unmeasured():
    os.environ["LOOKLOOK_EXECUTION_MODE"] = "INTRADAY_FAST"
    data_policy_bridge.install(data_metadata)
    performance_fast_path.install_fast_daily_metadata(data_metadata)
    now = datetime.now(base.CST)
    latest = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    context = {
        "status": "OK",
        "source": "History fast cache (Tencent qfq)",
        "errors": [],
        "latest_completed_date": latest,
        "previous_day": {"date": latest},
        "current_partial_bar": None,
        "cache": {
            "state": "FAST_REUSE_UNVERIFIED",
            "validation_key": (now - timedelta(days=1)).strftime("%Y-%m-%d") + ":closed",
            "validation_mode": "INCREMENTAL_VALIDATION",
        },
    }
    meta = data_metadata._daily_metadata(context, now.isoformat())
    assert meta["quality"] == "DEGRADED"
    assert "FAST_PATH_CACHE_REUSE_UNVERIFIED" in meta["quality_flags"]
    assert meta["freshness_sla"]["status"] == "UNMEASURED"
    assert meta["freshness_sla"]["reason"] == "SESSION_COMPLETENESS_UNVERIFIED"


def test_fast_breadth_is_cache_only_and_session_bounded():
    os.environ["LOOKLOOK_EXECUTION_MODE"] = "INTRADAY_FAST"
    now = datetime.now(base.CST).replace(microsecond=0)
    current_session = now.strftime("%Y-%m-%d")
    indices = {
        "上证指数": {"quote": {"market_time_cst": current_session + " 10:00:00"}},
        "深证成指": {"quote": {"market_time_cst": current_session + " 10:00:00"}},
    }

    with tempfile.TemporaryDirectory() as tmp:
        old_root = os.environ.get("MARKET_HISTORY_DIR")
        os.environ["MARKET_HISTORY_DIR"] = tmp
        root = Path(tmp)
        try:
            rel = "snapshots/latest.json"
            (root / "snapshots").mkdir(parents=True)
            breadth = {
                "status": "OK",
                "source": "Eastmoney clist full-universe",
                "collected_at_cst": (now - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S"),
                "market_session_date": current_session,
                "freshness": "LIVE",
                "reported_total_count": 5000,
                "covered_count": 5000,
                "overall": {"up_count": 3000, "down_count": 1800, "flat_count": 200},
            }
            (root / rel).write_text(
                json.dumps({"market_environment": {"breadth": breadth}}), encoding="utf-8"
            )
            (root / "manifest.json").write_text(
                json.dumps({"latest_snapshot": rel}), encoding="utf-8"
            )
            value = intraday_fast_tail.cache_only_market_breadth(base, now, indices)
            assert value["fast_path"]["source"] == "HISTORY_CACHE_ONLY"
            assert value["fast_path"]["network_refresh"] == "DEFERRED_OUTSIDE_CRITICAL_PATH"
            assert value["fast_path"]["age_seconds"] >= 120

            breadth["market_session_date"] = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            (root / rel).write_text(
                json.dumps({"market_environment": {"breadth": breadth}}), encoding="utf-8"
            )
            try:
                intraday_fast_tail.cache_only_market_breadth(base, now, indices)
            except RuntimeError as exc:
                assert "CACHE_SESSION_MISMATCH" in str(exc)
            else:
                raise AssertionError("cross-session breadth cache must not be reused")
        finally:
            if old_root is None:
                os.environ.pop("MARKET_HISTORY_DIR", None)
            else:
                os.environ["MARKET_HISTORY_DIR"] = old_root


def test_performance_summary_is_machine_readable_and_accurate():
    os.environ["LOOKLOOK_EXECUTION_MODE"] = "INTRADAY_FAST"
    performance_fast_path.reset_telemetry()
    performance_fast_path.record_stage("detail_stocks", 0.123)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(json.dumps({"schema_version": 13}), encoding="utf-8")
        intraday_fast_tail.finalize_performance(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        perf = data["performance"]
        assert perf["mode"] == "INTRADAY_FAST"
        assert perf["target_ms"] == 10000
        assert perf["hard_limit_ms"] == 15000
        assert perf["stages_ms"]["detail_stocks"] == 123.0
        assert "single Tencent Trust-B batch" in perf["fast_path_contract"]["indices"]
        assert "no network I/O" in perf["fast_path_contract"]["market_breadth"]
        assert perf["fast_path_contract"]["pdf_facts"] == "deferred to FULL"


def main():
    tests = [
        test_auto_mode_uses_market_window,
        test_fast_daily_cache_reuses_only_with_honest_state,
        test_fast_unverified_daily_metadata_is_degraded_and_unmeasured,
        test_fast_breadth_is_cache_only_and_session_bounded,
        test_performance_summary_is_machine_readable_and_accurate,
    ]
    for test in tests:
        test()
    print(f"INTRADAY_FAST_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
