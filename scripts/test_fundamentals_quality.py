import data_metadata
import data_policy
import data_policy_bridge
import fundamentals_context as fundamentals
import fundamentals_policy_bridge
import fundamentals_quality_bridge


fundamentals_policy_bridge.install(data_policy)
data_policy_bridge.install(data_metadata)
fundamentals_quality_bridge.install(fundamentals)


def _reported(date, kind, liabilities, cash, receivables, assets=1000, equity=700):
    return {
        "report_period_end": date,
        "period_key": f"{date[:4]}{kind}",
        "period_kind": kind,
        "balance_sheet": {
            "total_assets": assets,
            "total_liabilities": liabilities,
            "equity": equity,
            "cash": cash,
            "receivables": receivables,
            "inventory": 50,
            "goodwill": 10,
            "interest_bearing_debt": 100,
        },
        "profitability": {
            "weighted_roe_percent_reported": 8.0,
            "gross_margin_percent_reported": 40.0,
        },
    }


def test_cashflow_quality_states():
    strong = fundamentals_quality_bridge._cashflow_quality({
        "income": {"parent_net_profit": 100},
        "cashflow": {"operating_cash_flow": 120},
    })
    weak = fundamentals_quality_bridge._cashflow_quality({
        "income": {"parent_net_profit": 100},
        "cashflow": {"operating_cash_flow": 30},
    })
    divergent = fundamentals_quality_bridge._cashflow_quality({
        "income": {"parent_net_profit": 100},
        "cashflow": {"operating_cash_flow": -20},
    })
    assert strong["state"] == "STRONG"
    assert weak["state"] == "WEAK"
    assert divergent["state"] == "DIVERGENT"


def test_balance_growth_uses_same_period_prior_year():
    latest = _reported("2026-06-30", "H1", 360, 150, 150)
    prior = _reported("2025-06-30", "H1", 300, 200, 100)
    value = fundamentals_quality_bridge._balance_growth(fundamentals, latest, [latest, prior])
    assert value["status"] == "OK"
    assert round(value["yoy_percent"]["total_liabilities"], 4) == 20.0
    assert round(value["yoy_percent"]["cash"], 4) == -25.0
    assert round(value["yoy_percent"]["receivables"], 4) == 50.0


def test_roa_is_not_invented_from_period_end_assets():
    raw = {
        "main": [{
            "REPORTDATE": "2026-06-30",
            "NOTICE_DATE": "2026-08-01 18:00:00",
            "TOTAL_OPERATE_INCOME": 350,
            "PARENT_NETPROFIT": 50,
            "WEIGHTAVG_ROE": 8.0,
            "XSMLL": 40.0,
        }],
        "income": [{"REPORT_DATE": "2026-06-30", "TOTAL_OPERATE_INCOME": 350, "PARENT_NETPROFIT": 50}],
        "balance": [{"REPORT_DATE": "2026-06-30", "TOTAL_ASSETS": 1000, "TOTAL_LIABILITIES": 300}],
        "cashflow": [{"REPORT_DATE": "2026-06-30", "NETCASH_OPERATE": 60}],
    }
    context = fundamentals._build_context(
        "002558", {"events": {"recent": []}}, raw, {}, {}, [], "2026-08-01T18:05:00+08:00", "FULL"
    )
    roa = context["profitability_context"]["roa"]
    assert roa["status"] == "UNAVAILABLE"
    assert roa["value_percent"] is None
    assert "AVERAGE_ASSET" in roa["reason"]


def main():
    tests = [
        test_cashflow_quality_states,
        test_balance_growth_uses_same_period_prior_year,
        test_roa_is_not_invented_from_period_end_assets,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"FUNDAMENTALS_QUALITY_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
