import json
import tempfile
from pathlib import Path

import market_environment


INDEX_NAMES = ["上证指数", "沪深300", "中证1000", "深证成指", "创业板指", "科创50"]


def _breadth(up=4200, down=1000, flat=100, amount_1e8=28000.0, status="OK"):
    total = up + down + flat
    return {
        "status": status,
        "source": "fixture",
        "reported_total_count": total,
        "covered_count": total,
        "coverage_percent": 100.0,
        "overall": {
            "count": total,
            "change_covered_count": total,
            "up_count": up,
            "down_count": down,
            "flat_count": flat,
            "up_ratio_percent": round(up / total * 100, 2),
            "down_ratio_percent": round(down / total * 100, 2),
            "breadth_score_percent": round((up - down) / total * 100, 2),
            "amount_1e8": amount_1e8,
            "limit_up_count_approx": 80,
            "limit_down_count_approx": 5,
            "broken_limit_up_count_approx": 15,
        },
        "boards": {},
        "exchanges": {},
    }


def _snapshot(index_values, group_mean=1.2, group_breadth=50.0, target_pct=1.3, code="002558"):
    indices = {}
    for name, pct in zip(INDEX_NAMES, index_values):
        indices[name] = {
            "status": "OK",
            "quote": {
                "source": "Tencent",
                "latest": 3000,
                "change_percent": pct,
                "freshness": "LIVE",
                "market_time_cst": "2026-08-07 14:30:00",
            },
        }

    return {
        "schema_version": 8,
        "market_window": True,
        "indices": indices,
        "groups": {
            "game_sector": {
                "label": "A股游戏板块对照组",
                "status": "OK",
                "target": {"code": code, "name": "测试标的", "change_percent": target_pct},
                "coverage_percent": 100,
                "mean_change_percent": group_mean,
                "median_change_percent": group_mean,
                "up_count": 3 if group_breadth > 0 else 0,
                "down_count": 0 if group_breadth > 0 else 3,
                "flat_count": 0,
                "breadth_score_percent": group_breadth,
                "target_vs_peer_mean_percent": target_pct - group_mean,
            }
        },
        "detail_stocks": {
            code: {
                "quote": {
                    "name": "测试标的",
                    "change_percent": target_pct,
                    "freshness": "LIVE",
                },
                "intraday": {
                    "bias": "UPTREND",
                    "price_vs_vwap_percent": 0.6,
                },
            }
        },
        "quote_resilience": {"status": "OK"},
        "live_price_guard": {"status": "OK"},
    }


def test_broad_risk_on_market_driver():
    snapshot = _snapshot([1.0, 1.1, 1.7, 1.3, 1.6, 1.8], group_mean=1.3, target_pct=1.35)
    env = market_environment.build_market_environment(snapshot, _breadth())
    assert env["status"] == "OK"
    assert env["regime"]["status"] == "BROAD_RISK_ON"
    assert env["style"]["status"] in {"SMALL_GROWTH_LEADING", "SMALL_CAP_LEADING", "GROWTH_LEADING"}
    assert env["confidence"] == "HIGH"
    target = env["targets"]["002558"]
    assert target["driver_attribution"]["primary_driver"] == "MARKET"
    assert target["relative_strength"]["relative_to_market"] == "INLINE"
    assert target["intraday_context"]["bias"] == "UPTREND"
    assert "全市场上涨/下跌/平盘" in env["summary"]
    print("PASS broad_risk_on_market_driver")


def test_sector_driver_when_sector_diverges_from_market():
    snapshot = _snapshot([1.0, 1.1, 1.2, 1.0, 1.1, 1.2], group_mean=-0.8, group_breadth=-100.0, target_pct=-0.7)
    env = market_environment.build_market_environment(snapshot, _breadth(up=3500, down=1700, flat=100))
    target = env["targets"]["002558"]
    assert target["driver_attribution"]["primary_driver"] == "SECTOR"
    assert "STOCK_TRACKS_SECTOR" in target["driver_attribution"]["reason_codes"]
    assert target["relative_strength"]["relative_to_market"] == "UNDERPERFORM"
    print("PASS sector_driver")


def test_idiosyncratic_driver_when_stock_breaks_from_market_and_sector():
    snapshot = _snapshot([0.9, 1.0, 1.1, 1.0, 1.1, 1.0], group_mean=0.8, target_pct=-1.0)
    env = market_environment.build_market_environment(snapshot, _breadth())
    target = env["targets"]["002558"]
    assert target["driver_attribution"]["primary_driver"] == "IDIOSYNCRATIC"
    evidence = target["driver_attribution"]["evidence"]
    assert evidence["stock_vs_market_percent"] < -1.5
    assert evidence["stock_vs_sector_percent"] < -1.5
    print("PASS idiosyncratic_driver")


def test_stale_indices_are_not_counted_as_usable():
    snapshot = _snapshot([0.8, 0.9, 1.0, 1.1, 1.2, 1.3])
    snapshot["indices"]["科创50"]["quote"]["freshness"] = "STALE"
    env = market_environment.build_market_environment(snapshot, _breadth())
    assert env["indices"]["covered_count"] == 5
    assert env["indices"]["status"] == "PARTIAL"
    assert env["confidence"] == "MEDIUM"
    print("PASS stale_index_excluded")


def test_market_record_summary_and_limit_diagnostics():
    records = [
        {"f12": "600001", "f14": "主板A", "f3": 10.01, "f6": 100000000, "f2": 11, "f15": 11, "f18": 10},
        {"f12": "300001", "f14": "创业A", "f3": 3.0, "f6": 200000000, "f2": 10.3, "f15": 10.5, "f18": 10},
        {"f12": "688001", "f14": "科创A", "f3": -4.0, "f6": 300000000, "f2": 9.6, "f15": 10.0, "f18": 10},
        {"f12": "430001", "f14": "北证A", "f3": -30.0, "f6": 400000000, "f2": 7.0, "f15": 10.0, "f18": 10},
        {"f12": "000001", "f14": "ST测试", "f3": 4.99, "f6": 500000000, "f2": 10.5, "f15": 10.5, "f18": 10},
    ]
    summary = market_environment._summarize_market_records(records)
    assert summary["count"] == 5
    assert summary["up_count"] == 3
    assert summary["down_count"] == 2
    assert summary["amount_1e8"] == 15.0
    assert summary["limit_up_count_approx"] == 2
    assert summary["limit_down_count_approx"] == 1
    print("PASS market_record_summary")


def test_finalize_snapshot_updates_schema_and_feature():
    snapshot = _snapshot([0.2, 0.3, 0.4, 0.2, 0.3, 0.4])
    old_breadth = market_environment.LAST_BREADTH
    market_environment.LAST_BREADTH = _breadth(up=2800, down=2200, flat=200)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            market_environment.finalize_snapshot(path)
            data = json.loads(path.read_text(encoding="utf-8"))
    finally:
        market_environment.LAST_BREADTH = old_breadth

    assert data["schema_version"] == 9
    assert data["features"]["market_environment"] == "v1"
    assert data["market_environment"]["indices"]["covered_count"] == 6
    assert data["market_environment"]["breadth"]["overall"]["up_count"] == 2800
    assert data["market_environment"]["summary"]
    print("PASS finalize_snapshot_schema9")


def main():
    tests = [
        test_broad_risk_on_market_driver,
        test_sector_driver_when_sector_diverges_from_market,
        test_idiosyncratic_driver_when_stock_breaks_from_market_and_sector,
        test_stale_indices_are_not_counted_as_usable,
        test_market_record_summary_and_limit_diagnostics,
        test_finalize_snapshot_updates_schema_and_feature,
    ]
    for test in tests:
        test()
    print(f"MARKET_ENVIRONMENT_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
