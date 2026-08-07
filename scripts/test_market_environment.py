import json
import tempfile
import urllib.parse
from pathlib import Path

import market_breadth_source
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
        "estimated": status == "PARTIAL",
        "overall": {
            "estimated": status == "PARTIAL",
            "count": total,
            "change_covered_count": total,
            "unavailable_change_count": 0,
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


def test_broad_market_reference_excludes_style_indices():
    snapshot = _snapshot([1.0, 1.0, 8.0, 1.0, 8.0, 8.0], group_mean=1.0, target_pct=1.1)
    env = market_environment.build_market_environment(snapshot, _breadth())
    assert env["indices"]["mean_change_percent"] > 4.0
    assert env["indices"]["broad_market_reference_percent"] == 1.0
    target = env["targets"]["002558"]
    assert target["relative_strength"]["market_reference_percent"] == 1.0
    assert target["relative_strength"]["relative_to_market"] == "INLINE"
    print("PASS broad_market_reference_excludes_style_indices")


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
        {"f12": "000002", "f14": "停牌样本", "f3": None, "f6": 0, "f2": None, "f15": None, "f18": 10},
    ]
    summary = market_environment._summarize_market_records(records)
    assert summary["count"] == 6
    assert summary["change_covered_count"] == 5
    assert summary["unavailable_change_count"] == 1
    assert summary["up_count"] == 3
    assert summary["down_count"] == 2
    assert summary["flat_count"] == 0
    assert summary["amount_1e8"] == 15.0
    assert summary["limit_up_count_approx"] == 2
    assert summary["limit_down_count_approx"] == 1
    print("PASS market_record_summary")


def test_sample_unavailable_stays_separate_from_flat():
    records = []
    pcts = [1, 1, 1, 1, 1, 1, -1, -1, 0, None]
    for i, pct in enumerate(pcts):
        records.append(
            {"f13": 1, "f12": str(600000 + i), "f14": f"样本{i}", "f3": pct, "f6": 100000000, "f2": 10, "f15": 10, "f18": 10}
        )
    result = market_breadth_source._estimated_overall(records, 100)
    assert result["change_covered_count"] == 90
    assert result["unavailable_change_count"] == 10
    assert result["up_count"] == 60
    assert result["down_count"] == 20
    assert result["flat_count"] == 10
    assert result["sample_unavailable_change_count"] == 1
    print("PASS sampled_unavailable_separate")


def test_truncated_gainers_page_falls_back_to_systematic_sample():
    captured = []

    class FakeBase:
        @staticmethod
        def in_market_window(now):
            return True

        @staticmethod
        def http_get(url, timeout=0, attempts=0):
            captured.append(url)
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)
            if "82.push2.eastmoney.com" in parsed.netloc:
                rows = [
                    {"f13": 1, "f12": f"600{i:03d}", "f14": f"涨幅榜{i}", "f3": 8.0, "f6": 100000000, "f2": 10.8, "f15": 10.8, "f18": 10}
                    for i in range(100)
                ]
                return json.dumps({"data": {"total": 1600, "diff": rows}}, ensure_ascii=False)

            page = int(query.get("pn", ["1"])[0])
            page_size = int(query.get("pz", ["100"])[0])
            start = (page - 1) * page_size
            rows = []
            for offset in range(page_size):
                idx = start + offset
                if idx >= 1600:
                    break
                pct = 1.0 if idx % 4 != 3 else -1.0
                code = str(600000 + idx)
                rows.append(
                    {"f13": 1, "f12": code, "f14": f"样本{idx}", "f3": pct, "f6": 100000000, "f2": 10.1, "f15": 10.2, "f18": 10}
                )
            return json.dumps({"data": {"total": 1600, "diff": rows}}, ensure_ascii=False)

    from datetime import datetime

    result = market_breadth_source.fetch_market_breadth(FakeBase, datetime(2026, 8, 7, 14, 30, 0))
    assert result["status"] == "PARTIAL"
    assert result["estimated"] is True
    assert result["partial_reason"] == "FULL_UNIVERSE_UNAVAILABLE_USING_SYSTEMATIC_SAMPLE"
    assert result["sampling"]["method"] == "systematic_even_pages_sorted_by_stock_code"
    assert result["sampling"]["sample_count"] >= 700
    assert result["overall"]["estimated"] is True
    assert result["overall"]["up_ratio_percent"] == 75.0
    assert result["overall"]["down_ratio_percent"] == 25.0
    assert result["overall"]["unavailable_change_count"] == 0
    assert result["overall"]["amount_1e8"] is None
    assert result["limit_statistics"]["available"] is False
    assert any("82.push2.eastmoney.com" in url for url in captured)
    assert any("push2.eastmoney.com" in url and "fid=f12" in url for url in captured)

    env = market_environment.build_market_environment(
        _snapshot([1.0, 1.1, 1.2, 1.0, 1.1, 1.2]), result
    )
    assert env["status"] == "PARTIAL"
    assert env["regime"]["status"] == "BROAD_RISK_ON"
    assert env["regime"]["breadth_estimated"] is True
    assert env["confidence"] == "MEDIUM"
    assert "None 亿元" not in env["summary"]
    assert "总成交额在样本模式下不外推" in env["summary"]
    print("PASS biased_top_page_replaced_with_systematic_sample")


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
        test_broad_market_reference_excludes_style_indices,
        test_sector_driver_when_sector_diverges_from_market,
        test_idiosyncratic_driver_when_stock_breaks_from_market_and_sector,
        test_stale_indices_are_not_counted_as_usable,
        test_market_record_summary_and_limit_diagnostics,
        test_sample_unavailable_stays_separate_from_flat,
        test_truncated_gainers_page_falls_back_to_systematic_sample,
        test_finalize_snapshot_updates_schema_and_feature,
    ]
    for test in tests:
        test()
    print(f"MARKET_ENVIRONMENT_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
