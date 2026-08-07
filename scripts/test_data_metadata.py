import copy

import data_metadata


def _snapshot():
    return {
        "schema_version": 9,
        "runner_time_cst": "2026-08-07 14:30:00.000",
        "runner_time_utc": "2026-08-07T06:30:00+00:00",
        "market_window": True,
        "features": {},
        "detail_stocks": {
            "002558": {
                "code": "002558",
                "market": "SZ",
                "status": "OK",
                "quote": {
                    "source": "Tencent",
                    "latest": 29.34,
                    "change_percent": 0.27,
                    "market_time_cst": "2026-08-07 14:29:58",
                    "lag_seconds": 2,
                    "freshness": "LIVE",
                    "resilience": {
                        "fallback_used": True,
                        "consensus": {"status": "SINGLE_SOURCE"},
                        "providers": {
                            "Eastmoney": {"status": "ERROR", "usable": False},
                            "Tencent": {"status": "OK", "usable": True},
                        },
                    },
                },
                "minutes": {
                    "source": "Tencent",
                    "date": "20260807",
                    "freshness": "LIVE",
                    "count": 240,
                    "last_time": "1430",
                    "last_price": 29.34,
                },
                "intraday": {
                    "status": "OK",
                    "price": 29.34,
                    "bias": "UPTREND",
                },
                "daily_context": {
                    "status": "OK",
                    "source": "History cache (Tencent qfq)",
                    "errors": [],
                    "latest_completed_date": "2026-08-06",
                    "moving_averages": {
                        "ma5": 28.9,
                        "ma10": 28.4,
                        "ma20": 28.1,
                        "ma60": 25.0,
                    },
                    "atr14": 1.9,
                    "bars_last_60": [{"date": "2026-08-06", "close": 29.2}],
                    "cache": {"state": "HIT", "source": "Tencent qfq"},
                },
                "errors": [],
            }
        },
        "light_stocks": {
            "002555": {
                "status": "OK",
                "quote": {
                    "source": "Tencent",
                    "latest": 20.0,
                    "change_percent": -0.5,
                    "market_time_cst": "2026-08-07 14:29:58",
                    "lag_seconds": 2,
                    "freshness": "LIVE",
                },
            }
        },
        "groups": {
            "game": {
                "status": "OK",
                "requested_member_count": 5,
                "covered_member_count": 4,
                "coverage_percent": 80,
                "members": [
                    {"code": "002555", "available": True},
                    {"code": "002517", "available": True},
                    {"code": "300031", "available": True},
                    {"code": "300002", "available": True},
                    {"code": "300043", "available": False},
                ],
            }
        },
        "indices": {
            "上证指数": {
                "status": "OK",
                "quote": {
                    "source": "Eastmoney",
                    "latest": 3600,
                    "change_percent": 0.8,
                    "market_time_cst": "2026-08-07 14:29:58",
                    "lag_seconds": 2,
                    "freshness": "LIVE",
                },
            }
        },
        "history": {
            "storage": "market-data branch",
            "manifest": {"latest_runner_time_cst": "2026-08-07 14:30:00"},
            "daily_k_cache": {"002558": {"state": "HIT"}},
        },
        "live_price_guard": {
            "status": "OK",
            "hard_violation_count": 0,
            "warning_count": 0,
        },
        "quote_resilience": {
            "status": "DEGRADED",
            "fallback_count": 1,
            "divergent_count": 0,
            "unavailable_count": 0,
        },
        "market_environment": {
            "status": "PARTIAL",
            "confidence": "MEDIUM",
            "breadth": {
                "status": "PARTIAL",
                "estimated": True,
                "freshness": "LIVE",
                "market_session_date": "2026-08-07",
            },
        },
    }


def _clean_snapshot():
    data = _snapshot()
    quote = data["detail_stocks"]["002558"]["quote"]
    quote["source"] = "Eastmoney"
    quote["resilience"] = {
        "fallback_used": False,
        "consensus": {"status": "CONSISTENT"},
        "providers": {
            "Eastmoney": {"status": "OK", "usable": True},
            "Tencent": {"status": "OK", "usable": True},
        },
    }
    group = data["groups"]["game"]
    group["covered_member_count"] = 5
    group["coverage_percent"] = 100
    group["members"][-1]["available"] = True
    data["quote_resilience"].update(
        {"status": "OK", "fallback_count": 0, "divergent_count": 0, "unavailable_count": 0}
    )
    data["market_environment"] = {
        "status": "OK",
        "confidence": "HIGH",
        "breadth": {
            "status": "OK",
            "estimated": False,
            "freshness": "LIVE",
            "market_session_date": "2026-08-07",
        },
    }
    return data


def test_fallback_quote_metadata_is_explicit():
    data = data_metadata.decorate_snapshot(_snapshot())
    meta = data["detail_stocks"]["002558"]["quote"]["metadata"]
    assert meta["source"] == "Tencent"
    assert meta["source_tier"] == "SECONDARY_PROVIDER"
    assert meta["fallback_used"] is True
    assert meta["quality"] == "DEGRADED"
    assert "PRIMARY_SOURCE_FAILED" in meta["quality_flags"]
    assert "FALLBACK_USED" in meta["quality_flags"]
    assert meta["fetched_at"] == "2026-08-07T14:30:00+08:00"
    assert meta["data_time"] == "2026-08-07T14:29:58+08:00"
    print("PASS fallback_quote_metadata")


def test_session_minute_series_remains_valid_after_market_close():
    for freshness in ("CURRENT_SESSION", "LAST_SESSION"):
        source = _clean_snapshot()
        # This reproduces the real pipeline: base.detail_payload can still carry
        # PARTIAL from its pre-guard freshness interpretation, while the final
        # quote/minute nodes have already been normalized to a valid session.
        source["detail_stocks"]["002558"]["status"] = "PARTIAL"
        source["detail_stocks"]["002558"]["quote"]["freshness"] = freshness
        source["detail_stocks"]["002558"]["minutes"]["freshness"] = freshness
        data = data_metadata.decorate_snapshot(source)

        detail_meta = data["detail_stocks"]["002558"]["metadata"]
        minute_meta = data["detail_stocks"]["002558"]["minutes"]["metadata"]
        intraday_meta = data["detail_stocks"]["002558"]["intraday"]["metadata"]
        assert minute_meta["freshness"] == freshness
        assert minute_meta["quality"] == "PASS"
        assert minute_meta["confidence"] == "HIGH"
        assert "NOT_LIVE_NOW" in minute_meta["quality_flags"]
        assert intraday_meta["quality"] == "PASS"
        assert "INPUT_DATA_DEGRADED" not in intraday_meta["quality_flags"]
        assert detail_meta["quality"] == "PASS"
        assert data["data_quality"]["overall"] == "PASS"
    print("PASS closed_session_minute_quality")


def test_cache_and_derived_provenance_are_distinguishable():
    data = data_metadata.decorate_snapshot(_snapshot())
    daily = data["detail_stocks"]["002558"]["daily_context"]
    assert daily["metadata"]["source_tier"] == "CACHE"
    assert "HISTORY_CACHE_USED" in daily["metadata"]["quality_flags"]
    ma20 = daily["provenance"]["field_provenance"]["moving_averages.ma20"]
    assert ma20["algorithm"] == "SMA"
    assert ma20["period"] == 20
    assert ma20["derived_from"] == ["daily_k.close"]

    intraday = data["detail_stocks"]["002558"]["intraday"]
    assert intraday["metadata"]["source_tier"] == "DERIVED"
    assert intraday["provenance"]["algorithm"] == "intraday_structure_metrics_v1"
    print("PASS cache_and_derived_provenance")


def test_group_and_market_quality_are_visible_to_llm():
    data = data_metadata.decorate_snapshot(_snapshot())
    group = data["groups"]["game"]
    assert group["metadata"]["quality"] == "DEGRADED"
    assert "PEER_COVERAGE_INCOMPLETE" in group["metadata"]["quality_flags"]
    assert group["provenance"]["covered_member_count"] == 4

    env = data["market_environment"]
    assert env["metadata"]["source_tier"] == "DERIVED"
    assert "BREADTH_ESTIMATED" in env["metadata"]["quality_flags"]
    assert data["llm_data_summary"]["realtime_quote_quality"] == "MEDIUM"
    assert data["llm_data_summary"]["market_context_quality"] == "MEDIUM"
    print("PASS group_and_market_quality")


def test_missing_quote_becomes_critical_failure_without_hiding_error():
    source = _snapshot()
    source["detail_stocks"]["002558"]["quote"] = None
    data = data_metadata.decorate_snapshot(source)
    meta = data["detail_stocks"]["002558"]["quote_metadata"]
    assert meta["quality"] == "FAILED"
    assert meta["freshness"] == "UNAVAILABLE"
    assert "NO_VALID_DATA" in meta["quality_flags"]
    assert data["data_quality"]["overall"] == "FAILED"
    assert data["llm_data_summary"]["critical_data_ready"] is False
    assert any(
        x["path"] == "detail_stocks.002558.quote"
        for x in data["data_quality"]["critical_failures"]
    )
    assert not any(
        x["path"] == "detail_stocks.002558.quote"
        for x in data["data_quality"]["noncritical_failures"]
    )
    print("PASS missing_quote_critical_failure")


def test_noncritical_failed_node_cannot_yield_overall_pass():
    source = _clean_snapshot()
    source["indices"]["上证指数"] = {"status": "ERROR", "quote": None}
    data = data_metadata.decorate_snapshot(source)

    assert data["indices"]["上证指数"]["metadata"]["quality"] == "FAILED"
    assert data["data_quality"]["overall"] == "PARTIAL"
    assert data["llm_data_summary"]["critical_data_ready"] is True
    assert data["data_quality"]["quality_summary"]["FAILED"] >= 1
    assert any(
        x["path"] == "indices.上证指数" and x["quality"] == "FAILED"
        for x in data["data_quality"]["warnings"]
    )
    assert any(
        x["path"] == "indices.上证指数"
        for x in data["data_quality"]["noncritical_failures"]
    )
    assert any("indices.上证指数=FAILED" in x for x in data["llm_data_summary"]["warnings"])
    print("PASS noncritical_failed_node_is_partial")


def test_missing_minutes_sibling_metadata_is_in_quality_summary():
    source = _clean_snapshot()
    source["detail_stocks"]["002558"]["minutes"] = None
    data = data_metadata.decorate_snapshot(source)

    meta = data["detail_stocks"]["002558"]["minutes_metadata"]
    assert meta["quality"] == "FAILED"
    assert data["data_quality"]["quality_summary"]["FAILED"] >= 1
    assert any(
        x["path"] == "detail_stocks.002558.minutes"
        for x in data["data_quality"]["noncritical_failures"]
    )
    assert data["data_quality"]["overall"] == "PARTIAL"
    print("PASS sibling_missing_minutes_metadata_is_summarized")


def test_existing_snapshot_fields_are_not_removed():
    original = _snapshot()
    before = copy.deepcopy(original)
    data = data_metadata.decorate_snapshot(original)
    assert (
        data["detail_stocks"]["002558"]["quote"]["latest"]
        == before["detail_stocks"]["002558"]["quote"]["latest"]
    )
    assert (
        data["detail_stocks"]["002558"]["minutes"]["last_price"]
        == before["detail_stocks"]["002558"]["minutes"]["last_price"]
    )
    assert data["schema_version"] == 10
    assert data["features"]["data_provenance"] == "v1"
    assert "data_quality" in data
    assert "noncritical_failures" in data["data_quality"]
    assert "llm_data_summary" in data
    print("PASS snapshot_compatibility")


def main():
    tests = [
        test_fallback_quote_metadata_is_explicit,
        test_session_minute_series_remains_valid_after_market_close,
        test_cache_and_derived_provenance_are_distinguishable,
        test_group_and_market_quality_are_visible_to_llm,
        test_missing_quote_becomes_critical_failure_without_hiding_error,
        test_noncritical_failed_node_cannot_yield_overall_pass,
        test_missing_minutes_sibling_metadata_is_in_quality_summary,
        test_existing_snapshot_fields_are_not_removed,
    ]
    for test in tests:
        test()
    print(f"DATA_METADATA_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
