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


def test_normal_fast_cache_is_pass_quality():
    raw = {
        "main": [{"REPORTDATE": "2026-03-31", "NOTICE_DATE": "2026-04-30 18:00:00", "TOTAL_OPERATE_INCOME": 100, "PARENT_NETPROFIT": 10, "DEDUCT_PARENT_NETPROFIT": 9}],
        "income": [{"REPORT_DATE": "2026-03-31", "TOTAL_OPERATE_INCOME": 100, "PARENT_NETPROFIT": 10, "DEDUCT_PARENT_NETPROFIT": 9}],
        "balance": [{"REPORT_DATE": "2026-03-31", "TOTAL_ASSETS": 1000, "TOTAL_LIABILITIES": 300}],
        "cashflow": [{"REPORT_DATE": "2026-03-31", "NETCASH_OPERATE": 8}],
    }
    cache = {"fetched_at": "2026-05-01T10:00:00+08:00", "first_seen_by_version": {}}
    context = fundamentals._build_context(
        "002558", {"events": {"recent": []}}, raw, cache, {}, [],
        "2026-05-01T10:05:00+08:00", "INTRADAY_FAST",
    )
    assert context["status"] == "CACHED"
    assert context["metadata"]["quality"] == "PASS"
    assert "FAST_CACHE_ONLY" in context["metadata"]["quality_flags"]


def test_final_summary_recount_sees_fundamentals_upgrade():
    changes = {
        "stocks": {"002558": {"significance": "SIGNIFICANT"}},
        "groups": {},
        "summary": {"significant_changes": 0, "moderate_changes": 0, "minor_changes": 0},
    }
    changes_summary_finalizer.recount(changes)
    assert changes["summary"]["significant_changes"] == 1


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
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"FUNDAMENTALS_REVIEW_HARDENING_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
