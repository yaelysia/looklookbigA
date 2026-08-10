import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import data_metadata
import data_policy
import data_policy_bridge
import fundamentals_changes
import fundamentals_context as fundamentals
import fundamentals_policy_bridge


fundamentals_policy_bridge.install(data_policy)
data_policy_bridge.install(data_metadata)
CST = timezone(timedelta(hours=8))


def _period(date, revenue, profit, cfo, published="2026-04-30 18:00:00", adjusted=None, receivables=None, liabilities=None, cash=None):
    main = {
        "REPORTDATE": date,
        "NOTICE_DATE": published,
        "TOTAL_OPERATE_INCOME": revenue,
        "PARENT_NETPROFIT": profit,
        "DEDUCT_PARENT_NETPROFIT": adjusted,
        "YSTZ": 10.0,
        "SJLTZ": 20.0,
        "WEIGHTAVG_ROE": 8.0,
        "XSMLL": 40.0,
    }
    income = {"REPORT_DATE": date, "TOTAL_OPERATE_INCOME": revenue, "PARENT_NETPROFIT": profit, "OPERATE_PROFIT": profit * 1.2}
    cashflow = {"REPORT_DATE": date, "NETCASH_OPERATE": cfo}
    balance = {
        "REPORT_DATE": date,
        "TOTAL_ASSETS": 1000.0,
        "TOTAL_LIABILITIES": liabilities if liabilities is not None else 300.0,
        "MONETARYFUNDS": cash if cash is not None else 200.0,
        "ACCOUNTS_RECE": receivables if receivables is not None else 100.0,
        "INVENTORY": 50.0,
        "GOODWILL": 10.0,
        "TOTAL_EQUITY": 700.0,
    }
    return main, income, balance, cashflow


def _raw_periods(specs):
    raw = {key: [] for key in fundamentals.REPORTS}
    for spec in specs:
        values = _period(*spec)
        for key, value in zip(("main", "income", "balance", "cashflow"), values):
            raw[key].append(value)
    return raw


def test_verified_single_quarter_normalization_and_ttm():
    raw = _raw_periods([
        ("2025-03-31", 100, 10, 8),
        ("2025-06-30", 230, 25, 20),
        ("2025-09-30", 390, 45, 38),
        ("2025-12-31", 600, 70, 65),
        ("2026-03-31", 160, 20, 15),
        ("2026-06-30", 350, 50, 42),
    ])
    periods = fundamentals._normalize_reports(raw, {}, "2026-08-10T10:00:00+08:00")
    single = fundamentals._normalize_single_quarters(periods)
    by_key = {x["period_key"]: x for x in single}
    assert by_key["2025Q1"]["income"]["revenue"] == 100
    assert by_key["2025H1"]["income"]["revenue"] == 130
    assert by_key["2025Q3"]["income"]["revenue"] == 160
    assert by_key["2025FY"]["income"]["revenue"] == 210
    assert by_key["2026Q1"]["income"]["revenue"] == 160
    assert by_key["2026H1"]["income"]["revenue"] == 190
    assert by_key["2026H1"]["normalization"]["verified"] is True
    ttm = fundamentals._ttm(single)
    assert ttm["status"] == "OK"
    assert ttm["source_periods"] == ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
    assert ttm["income"]["revenue"] == 160 + 210 + 160 + 190


def test_missing_cumulative_predecessor_only_blocks_the_dependent_quarter():
    raw = _raw_periods([
        ("2026-06-30", 350, 50, 42),
        ("2026-09-30", 600, 90, 70),
    ])
    periods = fundamentals._normalize_reports(raw, {}, "2026-10-31T10:00:00+08:00")
    single = fundamentals._normalize_single_quarters(periods)
    by_key = {x["period_key"]: x for x in single}
    # H1 cannot be converted to Q2 without Q1. Q3, however, only needs H1:
    # Q3 single-quarter = Q3 cumulative - H1 cumulative.
    assert "2026H1" not in by_key
    assert by_key["2026Q3"]["income"]["revenue"] == 250
    assert by_key["2026Q3"]["normalization"]["source_periods"] == ["2026-09-30", "2026-06-30"]
    assert fundamentals._ttm(single)["status"] == "UNAVAILABLE"


def test_first_seen_is_stable_for_same_report_version():
    raw = _raw_periods([("2026-06-30", 350, 50, 42, "2026-08-01 18:00:00")])
    first_seen = {}
    one = fundamentals._normalize_reports(raw, first_seen, "2026-08-01T18:05:00+08:00")
    two = fundamentals._normalize_reports(raw, first_seen, "2026-08-10T10:00:00+08:00")
    assert one[0]["first_seen_at"] == "2026-08-01T18:05:00+08:00"
    assert two[0]["first_seen_at"] == one[0]["first_seen_at"]


def test_fundamentals_metadata_is_trust_b_and_discovery_sla_measured():
    latest = {
        "published_at": "2026-08-01T18:00:00+08:00",
        "first_seen_at": "2026-08-01T19:00:00+08:00",
    }
    meta = fundamentals._metadata(latest, "2026-08-01T19:01:00+08:00", "OK")
    assert meta["trust"]["tier"] == "B"
    assert meta["freshness_sla"]["data_class"] == "FUNDAMENTALS"
    assert meta["freshness_sla"]["status"] == "MET"
    assert meta["freshness_sla"]["observed_discovery_lag_seconds"] == 3600.0


def test_fast_path_is_cache_only():
    class Base:
        CST = CST
        calls = 0
        @staticmethod
        def http_get(url):
            Base.calls += 1
            raise AssertionError("FAST fundamentals must not issue network requests")

    old_root = os.environ.get("MARKET_HISTORY_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["MARKET_HISTORY_DIR"] = tmp
        raw = _raw_periods([
            ("2025-12-31", 600, 70, 65),
            ("2026-03-31", 160, 20, 15),
            ("2026-06-30", 350, 50, 42),
        ])
        cache = {
            "schema_version": 1,
            "code": "002558",
            "source": "Eastmoney",
            "source_tier": "PRIMARY_PROVIDER",
            "fetched_at": "2026-08-01T19:00:00+08:00",
            "first_seen_by_version": {},
            "source_urls": {},
            "raw": raw,
        }
        fundamentals._write_json(fundamentals._cache_path("002558"), cache)
        snapshot = {
            "schema_version": 14,
            "runner_time_cst": "2026-08-10T10:30:00+08:00",
            "detail_stocks": {"002558": {"events": {"recent": []}}},
        }
        path = Path(tmp) / "snapshot.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        fundamentals.finalize_snapshot(path, Base, "INTRADAY_FAST")
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data["detail_stocks"]["002558"]["fundamentals"]
        assert Base.calls == 0
        assert value["status"] == "CACHED"
        assert value["cache"]["state"] == "HIT"
        assert data["schema_version"] == 15
    if old_root is None:
        os.environ.pop("MARKET_HISTORY_DIR", None)
    else:
        os.environ["MARKET_HISTORY_DIR"] = old_root


def test_deterministic_divergence_signals():
    raw = _raw_periods([
        ("2025-06-30", 200, 20, 30, "2025-08-01 18:00:00", None, 80, 250, 220),
        ("2025-09-30", 330, 35, 45, "2025-10-30 18:00:00", None, 90, 270, 210),
        ("2025-12-31", 500, 50, 60, "2026-04-01 18:00:00", None, 100, 280, 200),
        ("2026-03-31", 160, 25, 10, "2026-04-30 18:00:00", None, 115, 300, 190),
        ("2026-06-30", 400, 60, 20, "2026-08-01 18:00:00", None, 150, 360, 150),
    ])
    periods = fundamentals._normalize_reports(raw, {}, "2026-08-01T18:10:00+08:00")
    single = fundamentals._normalize_single_quarters(periods)
    signals = {x["code"] for x in fundamentals._divergences(periods, single)}
    assert signals.issubset({
        "PROFIT_UP_CASHFLOW_DOWN", "MARGIN_DOWN_REVENUE_UP",
        "REVENUE_UP_RECEIVABLES_FASTER", "LEVERAGE_RISING_CASH_FALLING",
    })


def test_changes_distinguish_new_period_from_same_period_revision():
    before = {
        "latest_report_period_end": "2026-03-31",
        "single_quarters": [{"report_period_end": "2026-03-31", "income": {"revenue": 100, "parent_net_profit": 10}, "cashflow": {"operating_cash_flow": 8}}],
        "trends": {"revenue": "STABLE"},
        "divergence_signals": [],
    }
    after = {
        "latest_report_period_end": "2026-06-30",
        "single_quarters": [{"report_period_end": "2026-06-30", "income": {"revenue": 120, "parent_net_profit": 12}, "cashflow": {"operating_cash_flow": 9}}],
        "trends": {"revenue": "IMPROVING"},
        "divergence_signals": [{"code": "MARGIN_DOWN_REVENUE_UP"}],
    }
    change = fundamentals_changes.build_change(before, after)
    assert change["significance"] == "SIGNIFICANT"
    assert "NEW_FINANCIAL_REPORT_PERIOD" in change["reason_codes"]
    assert change["single_quarter_metrics"]["status"] == "NONCOMPARABLE_NEW_REPORT_PERIOD"

    revision = json.loads(json.dumps(before))
    revision["single_quarters"][0]["income"]["revenue"] = 105
    correction = fundamentals_changes.build_change(before, revision)
    assert correction["same_single_period_comparison"] is True
    assert correction["single_quarter_metrics"]["revenue"]["delta"] == 5.0
    assert "SAME_PERIOD_FINANCIAL_VALUES_UPDATED" in correction["reason_codes"]


def main():
    tests = [
        test_verified_single_quarter_normalization_and_ttm,
        test_missing_cumulative_predecessor_only_blocks_the_dependent_quarter,
        test_first_seen_is_stable_for_same_report_version,
        test_fundamentals_metadata_is_trust_b_and_discovery_sla_measured,
        test_fast_path_is_cache_only,
        test_deterministic_divergence_signals,
        test_changes_distinguish_new_period_from_same_period_revision,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"FUNDAMENTALS_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
