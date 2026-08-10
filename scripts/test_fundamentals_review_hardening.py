import json
import os
import tempfile
from datetime import timedelta, timezone
from pathlib import Path

import data_metadata
import data_policy
import data_policy_bridge
import fundamentals_cache_continuity
import fundamentals_changes
import fundamentals_context as fundamentals
import fundamentals_period_bridge
import fundamentals_policy_bridge
import fundamentals_quality_bridge
import fundamentals_review_hardening as hardening
import fundamentals_schema_bridge
import changes_summary_finalizer


fundamentals_policy_bridge.install(data_policy)
data_policy_bridge.install(data_metadata)
fundamentals_period_bridge.install(fundamentals)
fundamentals_quality_bridge.install(fundamentals)
fundamentals_schema_bridge.install(fundamentals)
hardening.install(fundamentals)


def test_debt_ratio_is_directional_not_improving():
    periods = []
    for date, kind, value in (
        ("2025-09-30", "Q3", 16.4042),
        ("2025-12-31", "FY", 18.6699),
        ("2026-03-31", "Q1", 19.8603),
    ):
        periods.append({
            "report_period_end": date,
            "period_key": f"{date[:4]}{kind}",
            "period_kind": kind,
            "balance_sheet": {"debt_to_assets_percent": value},
        })
    trend = hardening._point_in_time_direction(
        periods,
        lambda row: ((row or {}).get("balance_sheet") or {}).get("debt_to_assets_percent"),
        fundamentals,
    )
    assert trend["state"] == "RISING"
    assert trend["state"] != "IMPROVING"


def test_roe_uses_same_report_kind_prior_year_only():
    periods = [
        {"report_period_end": "2024-12-31", "period_key": "2024FY", "period_kind": "FY", "profitability": {"weighted_roe_percent_reported": 10.0}},
        {"report_period_end": "2025-03-31", "period_key": "2025Q1", "period_kind": "Q1", "profitability": {"weighted_roe_percent_reported": 5.0}},
        {"report_period_end": "2025-12-31", "period_key": "2025FY", "period_kind": "FY", "profitability": {"weighted_roe_percent_reported": 12.45}},
        {"report_period_end": "2026-03-31", "period_key": "2026Q1", "period_kind": "Q1", "profitability": {"weighted_roe_percent_reported": 6.81}},
    ]
    trend = hardening._same_period_trend(
        periods,
        lambda row: ((row or {}).get("profitability") or {}).get("weighted_roe_percent_reported"),
        fundamentals,
    )
    series = {row["period"]: row for row in trend["series"]}
    assert series["2025FY"]["prior_year_value_percent"] == 10.0
    assert series["2026Q1"]["prior_year_value_percent"] == 5.0
    assert trend["comparability"] == "SAME_REPORT_KIND_PRIOR_YEAR_ONLY"


def test_nonempty_provider_regression_preserves_newer_cache():
    fresh = [{"REPORT_DATE": "2026-03-31", "TOTAL_ASSETS": 900}]
    cached = [{"REPORT_DATE": "2026-06-30", "TOTAL_ASSETS": 1000}]
    merged, state = fundamentals_cache_continuity._merge_report_class(fresh, cached)
    assert state["status"] == "REGRESSED"
    assert merged[0]["REPORT_DATE"] == "2026-06-30"
    assert merged[0]["TOTAL_ASSETS"] == 1000


def test_shorter_same_latest_window_preserves_older_history():
    fresh = [
        {"REPORT_DATE": "2026-06-30", "TOTAL_ASSETS": 1100},
        {"REPORT_DATE": "2026-03-31", "TOTAL_ASSETS": 1050},
    ]
    cached = [
        {"REPORT_DATE": "2026-06-30", "TOTAL_ASSETS": 1000},
        {"REPORT_DATE": "2026-03-31", "TOTAL_ASSETS": 990},
        {"REPORT_DATE": "2025-12-31", "TOTAL_ASSETS": 950},
    ]
    merged, state = fundamentals_cache_continuity._merge_report_class(fresh, cached)
    assert state["status"] == "MERGED"
    assert [row["REPORT_DATE"] for row in merged] == ["2026-06-30", "2026-03-31", "2025-12-31"]
    assert merged[0]["TOTAL_ASSETS"] == 1100


def test_report_period_regression_is_not_new_report():
    before = {"latest_report_period_end": "2026-06-30", "single_quarters": [], "trends": {}, "signals": []}
    after = {"latest_report_period_end": "2026-03-31", "single_quarters": [], "trends": {}, "signals": []}
    change = fundamentals_changes.build_change(before, after)
    assert "NEW_FINANCIAL_REPORT_PERIOD" not in change["reason_codes"]
    assert "FINANCIAL_REPORT_PERIOD_REGRESSED" in change["reason_codes"]
    assert change["latest_report_period"]["progression"] == "REGRESSED"


def test_ttm_is_partial_when_one_core_field_missing():
    rows = []
    for index, (date, kind) in enumerate((
        ("2025-06-30", "Q2"),
        ("2025-09-30", "Q3"),
        ("2025-12-31", "Q4"),
        ("2026-03-31", "Q1"),
    )):
        revenue = None if index == 1 else 100.0 + index
        verified_fields = {
            "revenue": revenue is not None,
            "parent_net_profit": True,
            "adjusted_net_profit": True,
            "operating_cash_flow": True,
        }
        rows.append({
            "report_period_end": date,
            "period_kind": kind,
            "period_key": f"{date[:4]}{kind}",
            "income": {"revenue": revenue, "parent_net_profit": 10.0, "adjusted_net_profit": 9.0},
            "cashflow": {"operating_cash_flow": 8.0},
            "normalization": {"verified_fields": verified_fields},
        })
    value = fundamentals._ttm(rows)
    assert value["status"] == "PARTIAL"
    assert value["income"]["revenue"] is None
    assert value["field_availability"]["revenue"]["status"] == "UNAVAILABLE"
    assert "revenue" in value["missing_core_fields"]


def _healthy_raw(period="2026-03-31"):
    return {
        "main": [{"REPORTDATE": period, "NOTICE_DATE": "2026-04-30 18:00:00", "TOTAL_OPERATE_INCOME": 100, "PARENT_NETPROFIT": 10, "DEDUCT_PARENT_NETPROFIT": 9}],
        "income": [{"REPORT_DATE": period, "TOTAL_OPERATE_INCOME": 100, "PARENT_NETPROFIT": 10, "DEDUCT_PARENT_NETPROFIT": 9}],
        "balance": [{"REPORT_DATE": period, "TOTAL_ASSETS": 1000, "TOTAL_LIABILITIES": 300}],
        "cashflow": [{"REPORT_DATE": period, "NETCASH_OPERATE": 8}],
    }


def test_normal_fast_cache_is_pass_quality():
    raw = _healthy_raw()
    cache = {"fetched_at": "2026-05-01T10:00:00+08:00", "first_seen_by_version": {}}
    context = fundamentals._build_context(
        "002558", {"events": {"recent": []}}, raw, cache, {}, [],
        "2026-05-01T10:05:00+08:00", "INTRADAY_FAST",
    )
    assert context["status"] == "CACHED"
    assert context["metadata"]["quality"] == "PASS"
    assert "FAST_CACHE_ONLY" in context["metadata"]["quality_flags"]
    assert context["coverage"]["latest_report_period_complete"] is True


def test_final_summary_recount_sees_fundamentals_upgrade():
    changes = {
        "stocks": {"002558": {"significance": "SIGNIFICANT"}},
        "groups": {},
        "summary": {"significant_changes": 0, "moderate_changes": 0, "minor_changes": 0},
    }
    changes_summary_finalizer.recount(changes)
    assert changes["summary"]["significant_changes"] == 1


def test_empty_provider_result_is_explicit_error():
    class Base:
        @staticmethod
        def http_get(url):
            return json.dumps({"result": {"data": []}})

    try:
        fundamentals._fetch_report(Base, "002558", "SYNTHETIC", "REPORT_DATE")
        raise AssertionError("empty provider result must raise")
    except RuntimeError as exc:
        assert "EMPTY_OR_MISSING_REPORT_RESULT" in str(exc)


def test_cold_full_missing_latest_report_class_is_partial():
    raw = _healthy_raw("2026-06-30")
    raw["balance"] = []
    raw["cashflow"] = []
    context = fundamentals._build_context(
        "002558", {"events": {"recent": []}}, raw,
        {"first_seen_by_version": {}}, {}, [],
        "2026-08-10T10:05:00+08:00", "FULL",
    )
    assert context["latest_report_period_end"] == "2026-06-30"
    assert context["status"] == "PARTIAL"
    assert context["metadata"]["quality"] == "DEGRADED"
    assert context["provider_health"]["status"] == "PARTIAL"
    assert context["coverage"]["latest_report_period_complete"] is False
    assert context["coverage"]["missing_latest_report_classes"] == ["balance", "cashflow"]
    assert "INCOMPLETE_LATEST_REPORT_CLASS_COVERAGE" in context["metadata"]["quality_flags"]


def test_fast_cache_stale_latest_report_class_is_degraded():
    raw = _healthy_raw("2026-06-30")
    raw["balance"] = [{"REPORT_DATE": "2026-03-31", "TOTAL_ASSETS": 900, "TOTAL_LIABILITIES": 280}]
    raw["cashflow"] = [{"REPORT_DATE": "2026-03-31", "NETCASH_OPERATE": 7}]
    cache = {"fetched_at": "2026-07-01T10:00:00+08:00", "first_seen_by_version": {}}
    context = fundamentals._build_context(
        "002558", {"events": {"recent": []}}, raw, cache, {}, [],
        "2026-07-01T10:05:00+08:00", "INTRADAY_FAST",
    )
    assert context["status"] == "CACHED"
    assert context["metadata"]["quality"] == "DEGRADED"
    assert context["provider_health"]["status"] == "PARTIAL"
    assert context["coverage"]["latest_report_period_complete"] is False
    assert context["coverage"]["stale_latest_report_classes"] == ["balance", "cashflow"]
    assert "STALE_LATEST_REPORT_CLASS_BALANCE" in context["metadata"]["quality_flags"]


def test_finalize_healthy_fast_summary_is_ok():
    old_root = os.environ.get("MARKET_HISTORY_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["MARKET_HISTORY_DIR"] = tmp
        fundamentals._write_json(fundamentals._cache_path("002558"), {
            "schema_version": 1,
            "code": "002558",
            "source": "Eastmoney",
            "source_tier": "PRIMARY_PROVIDER",
            "fetched_at": "2026-05-01T10:00:00+08:00",
            "first_seen_by_version": {},
            "source_urls": {},
            "raw": _healthy_raw(),
        })
        snapshot_path = Path(tmp) / "snapshot.json"
        snapshot_path.write_text(json.dumps({
            "runner_time_cst": "2026-05-01T10:05:00+08:00",
            "detail_stocks": {"002558": {"events": {"recent": []}}},
        }), encoding="utf-8")

        class Base:
            CST = timezone(timedelta(hours=8))

        fundamentals.finalize_snapshot(snapshot_path, Base, "INTRADAY_FAST")
        result = json.loads(snapshot_path.read_text(encoding="utf-8"))
        context = result["detail_stocks"]["002558"]["fundamentals"]
        assert context["status"] == "CACHED"
        assert context["metadata"]["quality"] == "PASS"
        assert result["fundamentals_summary"]["status"] == "OK"
        assert result["fundamentals_summary"]["health_by_code"]["002558"] == "OK"
        assert result["fundamentals_summary"]["quality_by_code"]["002558"] == "PASS"

    if old_root is None:
        os.environ.pop("MARKET_HISTORY_DIR", None)
    else:
        os.environ["MARKET_HISTORY_DIR"] = old_root


def main():
    tests = [
        test_debt_ratio_is_directional_not_improving,
        test_roe_uses_same_report_kind_prior_year_only,
        test_nonempty_provider_regression_preserves_newer_cache,
        test_shorter_same_latest_window_preserves_older_history,
        test_report_period_regression_is_not_new_report,
        test_ttm_is_partial_when_one_core_field_missing,
        test_normal_fast_cache_is_pass_quality,
        test_final_summary_recount_sees_fundamentals_upgrade,
        test_empty_provider_result_is_explicit_error,
        test_cold_full_missing_latest_report_class_is_partial,
        test_fast_cache_stale_latest_report_class_is_degraded,
        test_finalize_healthy_fast_summary_is_ok,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"FUNDAMENTALS_REVIEW_HARDENING_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
