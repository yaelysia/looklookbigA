import fundamentals_context as fundamentals
import fundamentals_quality_bridge
import fundamentals_schema_bridge


fundamentals_quality_bridge.install(fundamentals)
fundamentals_schema_bridge.install(fundamentals)


def _raw():
    periods = [
        ("2025-03-31", 100, 10, 8, 7.0, 40.0, 100, 50, 10, 300),
        ("2025-06-30", 230, 25, 20, 7.5, 40.5, 110, 55, 10, 310),
        ("2025-09-30", 390, 45, 38, 8.0, 41.0, 120, 60, 10, 320),
        ("2025-12-31", 600, 70, 65, 8.5, 41.5, 130, 65, 10, 330),
        ("2026-03-31", 160, 20, 15, 7.5, 39.0, 125, 60, 15, 350),
        ("2026-06-30", 370, 48, 35, 6.0, 36.0, 180, 90, 30, 410),
    ]
    raw = {key: [] for key in fundamentals.REPORTS}
    for date, revenue, profit, cfo, roe, margin, receivables, inventory, goodwill, liabilities in periods:
        raw["main"].append({
            "REPORTDATE": date,
            "NOTICE_DATE": date[:4] + "-08-01 18:00:00",
            "TOTAL_OPERATE_INCOME": revenue,
            "PARENT_NETPROFIT": profit,
            "DEDUCT_PARENT_NETPROFIT": profit * 0.9,
            "YSTZ": 20.0,
            "SJLTZ": 20.0,
            "WEIGHTAVG_ROE": roe,
            "XSMLL": margin,
        })
        raw["income"].append({"REPORT_DATE": date, "TOTAL_OPERATE_INCOME": revenue, "PARENT_NETPROFIT": profit, "DEDUCT_PARENT_NETPROFIT": profit * 0.9, "OPERATE_PROFIT": profit * 1.1})
        raw["cashflow"].append({"REPORT_DATE": date, "NETCASH_OPERATE": cfo})
        raw["balance"].append({
            "REPORT_DATE": date,
            "TOTAL_ASSETS": 1000,
            "TOTAL_LIABILITIES": liabilities,
            "TOTAL_EQUITY": 1000 - liabilities,
            "MONETARYFUNDS": 200,
            "ACCOUNTS_RECE": receivables,
            "INVENTORY": inventory,
            "GOODWILL": goodwill,
        })
    return raw


def _context(events=None):
    return fundamentals._build_context(
        "002558", {"events": {"recent": events or []}}, _raw(), {},
        {"main": "https://example.invalid"}, [],
        "2026-08-10T10:00:00+08:00", "FULL",
    )


def test_trends_keep_series_and_evidence():
    context = _context()
    trends = context["trends"]
    for key in (
        "revenue_growth", "profit_growth", "adjusted_profit_growth", "net_margin",
        "adjusted_net_margin", "gross_margin", "roe", "cashflow_growth", "debt_ratio",
    ):
        assert isinstance(trends[key]["series"], list)
        assert isinstance(trends[key]["evidence"], list)
        assert trends[key]["state"] in {
            "ACCELERATING", "IMPROVING", "STABLE", "SLOWING",
            "DETERIORATING", "VOLATILE", "UNKNOWN",
        }
    assert context["quarterly_history"] == context["single_quarters"][:8]
    assert context["profitability"]["adjusted_net_margin"] == trends["adjusted_net_margin"]
    assert context["balance_sheet"]["debt_ratio_trend"] == trends["debt_ratio"]


def test_period_freshness_and_value_type_are_explicit():
    context = _context()
    reported = context["reported_periods"]
    assert reported[0]["freshness"] == "LATEST_REPORT"
    assert reported[1]["freshness"] == "PREVIOUS_REPORT"
    assert all(x["freshness"] == "HISTORICAL" for x in reported[2:])
    assert all(x["value_type"] == "REPORTED_CUMULATIVE" for x in reported)
    assert all(x["source"] == "Eastmoney" for x in reported)
    for item in context["income"]["single_quarter_history"]:
        assert item["value_type"] == "NORMALIZED_SINGLE_QUARTER"
        assert item["freshness"] in {"LATEST_REPORT", "PREVIOUS_REPORT", "HISTORICAL"}
        assert item["source"] == "Eastmoney"
        assert "adjusted_net_margin_percent" in item


def test_latest_report_separates_period_publish_fetch_and_revision_unknown():
    latest = _context()["latest_report"]
    assert latest["period_end_date"] == "2026-06-30"
    assert latest["published_at"]
    assert latest["first_seen_at"] == "2026-08-10T10:00:00+08:00"
    assert latest["fetched_at"] == "2026-08-10T10:00:00+08:00"
    assert latest["restated"] is None
    assert latest["revised"] is None
    assert latest["revision_status"].startswith("UNKNOWN_")


def test_signals_use_standard_attention_contract():
    context = _context()
    for signal in context["signals"]:
        assert signal["status"] == "ATTENTION"
        assert signal["reason_code"]
        assert isinstance(signal["evidence"], dict)
        assert isinstance(signal["periods"], list)
        assert signal["values"] == signal["evidence"]
        assert "not a fraud" in signal["semantic_note"]


def test_preliminary_events_never_replace_reported_history():
    event = {
        "event_id": "cninfo:prelim",
        "event_type": "EARNINGS_FORECAST",
        "published_at": "2026-07-10T18:00:00+08:00",
    }
    context = _context([event])
    prelim = context["preliminary_financial_events"]
    assert len(prelim) == 1
    assert prelim[0]["status"] == "PRELIMINARY_OR_FORECAST"
    assert context["reported_periods"]


def main():
    tests = [
        test_trends_keep_series_and_evidence,
        test_period_freshness_and_value_type_are_explicit,
        test_latest_report_separates_period_publish_fetch_and_revision_unknown,
        test_signals_use_standard_attention_contract,
        test_preliminary_events_never_replace_reported_history,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"FUNDAMENTALS_SCHEMA_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
