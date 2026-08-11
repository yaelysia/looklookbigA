import fundamentals_context as fundamentals
import fundamentals_period_bridge


fundamentals_period_bridge.install(fundamentals)


def _raw():
    rows = {
        "main": [],
        "income": [],
        "balance": [],
        "cashflow": [],
    }
    values = [
        ("2025-03-31", 100),
        ("2025-06-30", 230),
        ("2025-09-30", 390),
        ("2025-12-31", 600),
        ("2026-03-31", 160),
        ("2026-06-30", 350),
    ]
    for date, revenue in values:
        rows["main"].append({"REPORTDATE": date, "TOTAL_OPERATE_INCOME": revenue, "PARENT_NETPROFIT": revenue / 10})
        rows["income"].append({"REPORT_DATE": date, "TOTAL_OPERATE_INCOME": revenue, "PARENT_NETPROFIT": revenue / 10})
        rows["balance"].append({"REPORT_DATE": date, "TOTAL_ASSETS": 1000})
        rows["cashflow"].append({"REPORT_DATE": date, "NETCASH_OPERATE": revenue / 20})
    return rows


def test_single_quarters_use_q1_q2_q3_q4_labels():
    periods = fundamentals._normalize_reports(_raw(), {}, "2026-08-10T10:00:00+08:00")
    single = fundamentals._normalize_single_quarters(periods)
    by_key = {x["period_key"]: x for x in single}
    assert by_key["2025Q1"]["income"]["revenue"] == 100
    assert by_key["2025Q2"]["income"]["revenue"] == 130
    assert by_key["2025Q3"]["income"]["revenue"] == 160
    assert by_key["2025Q4"]["income"]["revenue"] == 210
    assert by_key["2026Q1"]["income"]["revenue"] == 160
    assert by_key["2026Q2"]["income"]["revenue"] == 190
    assert by_key["2025Q2"]["source_report_kind"] == "H1"
    assert by_key["2025Q4"]["source_report_kind"] == "FY"
    assert by_key["2025Q2"]["normalization"]["normalized_period_kind"] == "Q2"


def test_ttm_requires_consecutive_normalized_quarter_labels():
    periods = fundamentals._normalize_reports(_raw(), {}, "2026-08-10T10:00:00+08:00")
    single = fundamentals._normalize_single_quarters(periods)
    ttm = fundamentals._ttm(single)
    assert ttm["status"] == "OK"
    assert ttm["source_periods"] == ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]


def test_missing_q1_only_blocks_q2_not_q3():
    raw = {"main": [], "income": [], "balance": [], "cashflow": []}
    for date, revenue in (("2026-06-30", 350), ("2026-09-30", 600)):
        raw["main"].append({"REPORTDATE": date, "TOTAL_OPERATE_INCOME": revenue, "PARENT_NETPROFIT": revenue / 10})
        raw["income"].append({"REPORT_DATE": date, "TOTAL_OPERATE_INCOME": revenue, "PARENT_NETPROFIT": revenue / 10})
        raw["balance"].append({"REPORT_DATE": date, "TOTAL_ASSETS": 1000})
        raw["cashflow"].append({"REPORT_DATE": date, "NETCASH_OPERATE": revenue / 20})
    periods = fundamentals._normalize_reports(raw, {}, "2026-10-31T10:00:00+08:00")
    single = fundamentals._normalize_single_quarters(periods)
    by_key = {x["period_key"]: x for x in single}
    assert "2026Q2" not in by_key
    assert by_key["2026Q3"]["income"]["revenue"] == 250


def main():
    tests = [
        test_single_quarters_use_q1_q2_q3_q4_labels,
        test_ttm_requires_consecutive_normalized_quarter_labels,
        test_missing_q1_only_blocks_q2_not_q3,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"FUNDAMENTALS_PERIOD_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
