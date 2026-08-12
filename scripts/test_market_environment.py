import json
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path

import market_breadth_source
import market_environment


INDEX_NAMES = ["上证指数", "沪深300", "中证1000", "深证成指", "创业板指", "科创50"]


def _breadth(up=4200, down=1000, flat=100, amount_1e8=28000.0, status="OK", freshness="LIVE"):
    total = up + down + flat
    return {
        "status": status,
        "source": "fixture",
        "collected_at_cst": "2026-08-07 14:30:00",
        "market_session_date": "2026-08-07",
        "freshness": freshness,
        "freshness_basis": "INDEX_QUOTE_SESSION_ANCHOR",
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


def _snapshot(
    index_values,
    group_mean=1.2,
    group_breadth=50.0,
    target_pct=1.3,
    code="002558",
    requested_members=8,
    covered_members=None,
    group_status=None,
):
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

    covered_members = requested_members if covered_members is None else covered_members
    coverage = covered_members / requested_members * 100.0 if requested_members else 0.0
    group_status = group_status or ("OK" if coverage >= 75 else ("PARTIAL" if covered_members else "ERROR"))

    return {
        "schema_version": 8,
        "market_window": True,
        "indices": indices,
        "groups": {
            "game_sector": {
                "label": "A股游戏板块对照组",
                "status": group_status,
                "target": {"code": code, "name": "测试标的", "change_percent": target_pct},
                "requested_member_count": requested_members,
                "active_member_count": requested_members,
                "covered_member_count": covered_members,
                "coverage_percent": coverage,
                "mean_change_percent": group_mean,
                "median_change_percent": group_mean,
                "up_count": covered_members if group_breadth > 0 else 0,
                "down_count": 0 if group_breadth > 0 else covered_members,
                "flat_count": 0,
                "breadth_score_percent": group_breadth,
                "target_vs_peer_mean_percent": target_pct - group_mean,
                "members": [
                    {"code": f"peer{i}", "available": i < covered_members}
                    for i in range(requested_members)
                ],
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


def _anchor_indices(date="2026-08-07", freshness="LIVE"):
    return {
        "上证指数": {
            "status": "OK",
            "quote": {"market_time_cst": f"{date} 15:00:00", "freshness": freshness},
        },
        "沪深300": {
            "status": "OK",
            "quote": {"market_time_cst": f"{date} 15:00:01", "freshness": freshness},
        },
        "深证成指": {
            "status": "OK",
            "quote": {"market_time_cst": f"{date} 15:00:02", "freshness": freshness},
        },
    }


def _page_rows(page, page_size=100, total=1600):
    start = (page - 1) * page_size
    rows = []
    for offset in range(page_size):
        idx = start + offset
        if idx >= total:
            break
        pct = 1.0 if idx % 4 != 3 else -1.0
        rows.append(
            {
                "f13": 1,
                "f12": str(600000 + idx),
                "f14": f"样本{idx}",
                "f3": pct,
                "f6": 100000000,
                "f2": 10.1,
                "f15": 10.2,
                "f18": 10,
            }
        )
    return rows


def test_broad_risk_on_market_driver():
    snapshot = _snapshot([1.0, 1.1, 1.7, 1.3, 1.6, 1.8], group_mean=1.3, target_pct=1.35)
    env = market_environment.build_market_environment(snapshot, _breadth())
    assert env["status"] == "OK"
    assert env["regime"]["status"] == "BROAD_RISK_ON"
    assert env["style"]["status"] in {"SMALL_GROWTH_LEADING", "SMALL_CAP_LEADING", "GROWTH_LEADING"}
    assert env["confidence"] == "HIGH"
    target = env["targets"]["002558"]
    assert target["driver_attribution"]["primary_driver"] == "MARKET"
    assert target["driver_attribution"]["confidence"] in {"MEDIUM", "HIGH"}
    assert target["relative_strength"]["relative_to_market"] == "INLINE"
    assert target["intraday_context"]["bias"] == "UPTREND"
    assert "全市场上涨/下跌/平盘" in env["summary"]
    print("PASS broad_risk_on_market_driver")


def test_broad_market_reference_excludes_style_indices():
    snapshot = _snapshot([1.0, 1.0, 8.0, 1.0, 8.0, 8.0], group_mean=1.0, target_pct=1.1)
    env = market_environment.build_market_environment(snapshot, _breadth())
    assert env["indices"]["mean_change_percent"] > 4.0
    assert env["indices"]["broad_market_reference_percent"] == 1.0
    assert env["indices"]["broad_market_reference_quality"] == "HIGH"
    target = env["targets"]["002558"]
    assert target["relative_strength"]["market_reference_percent"] == 1.0
    assert target["relative_strength"]["relative_to_market"] == "INLINE"
    print("PASS broad_market_reference_excludes_style_indices")


def test_sector_driver_high_requires_quality_and_separation():
    snapshot = _snapshot(
        [1.0, 1.1, 1.2, 1.0, 1.1, 1.2],
        group_mean=-0.8,
        group_breadth=-100.0,
        target_pct=-0.7,
        requested_members=10,
        covered_members=8,
        group_status="OK",
    )
    env = market_environment.build_market_environment(snapshot, _breadth(up=3500, down=1700, flat=100))
    attribution = env["targets"]["002558"]["driver_attribution"]
    assert attribution["primary_driver"] == "SECTOR"
    assert attribution["confidence"] == "HIGH"
    assert attribution["sector_reference_quality"]["quality"] == "HIGH"
    assert attribution["sector_reference_quality"]["covered_member_count"] == 8
    assert attribution["sector_reference_quality"]["coverage_percent"] == 80.0
    assert attribution["classification_separation"]["quality"] == "HIGH"
    assert "STOCK_TRACKS_SECTOR" in attribution["reason_codes"]
    print("PASS sector_driver_high_quality")


def test_sector_driver_single_peer_is_low_confidence():
    snapshot = _snapshot(
        [1.0, 1.1, 1.2, 1.0, 1.1, 1.2],
        group_mean=-0.8,
        group_breadth=-100.0,
        target_pct=-0.7,
        requested_members=1,
        covered_members=1,
        group_status="OK",
    )
    attribution = market_environment.build_market_environment(snapshot, _breadth())["targets"]["002558"]["driver_attribution"]
    assert attribution["primary_driver"] == "SECTOR"
    assert attribution["sector_reference_quality"]["quality"] == "LOW"
    assert attribution["confidence"] == "LOW"
    print("PASS sector_single_peer_low_confidence")


def test_sector_driver_four_peers_is_capped_medium():
    snapshot = _snapshot(
        [1.0, 1.1, 1.2, 1.0, 1.1, 1.2],
        group_mean=-0.8,
        group_breadth=-100.0,
        target_pct=-0.7,
        requested_members=4,
        covered_members=4,
        group_status="OK",
    )
    attribution = market_environment.build_market_environment(snapshot, _breadth())["targets"]["002558"]["driver_attribution"]
    assert attribution["primary_driver"] == "SECTOR"
    assert attribution["sector_reference_quality"]["quality"] == "MEDIUM"
    assert attribution["confidence"] == "MEDIUM"
    print("PASS sector_four_peers_medium_confidence")


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


def test_stratified_sample_uses_neighbor_inside_failed_target_stratum():
    calls = []

    class FakeBase:
        @staticmethod
        def http_get(url, timeout=0, attempts=0):
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)
            page = int(query.get("pn", ["1"])[0])
            calls.append((parsed.netloc, page))
            if "82.push2.eastmoney.com" in parsed.netloc:
                return json.dumps({"data": {"total": 1600, "diff": _page_rows(1)}}, ensure_ascii=False)
            if page == 11:
                raise OSError("forced target-page failure")
            return json.dumps({"data": {"total": 1600, "diff": _page_rows(page)}}, ensure_ascii=False)

    result = market_breadth_source.fetch_market_breadth(
        FakeBase,
        datetime(2026, 8, 7, 14, 30, 0),
        _anchor_indices(),
    )
    assert result["status"] == "PARTIAL"
    assert result["estimated"] is True
    assert result["sampling"]["method"] == "complete_stratified_code_rank_sample"
    assert result["sampling"]["required_strata_count"] == 8
    assert result["sampling"]["successful_strata_count"] == 8
    assert result["sampling"]["all_strata_covered"] is True
    failed_target_stratum = [x for x in result["sampling"]["strata"] if x["target_page"] == 11][0]
    assert failed_target_stratum["selected_page"] == 12
    assert failed_target_stratum["fallback_used"] is True
    assert failed_target_stratum["attempted_pages"] == [11, 12]
    assert result["overall"]["count_semantics"] == "estimated_from_complete_stratified_code_rank_sample"
    assert result["freshness"] == "LIVE"
    assert result["market_session_date"] == "2026-08-07"
    print("PASS stratum_neighbor_fallback")


def test_missing_tail_stratum_rejects_extrapolation_even_with_large_sample():
    class FakeBase:
        @staticmethod
        def http_get(url, timeout=0, attempts=0):
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)
            page = int(query.get("pn", ["1"])[0])
            if "82.push2.eastmoney.com" in parsed.netloc:
                return json.dumps({"data": {"total": 1600, "diff": _page_rows(1)}}, ensure_ascii=False)
            if page in {15, 16}:
                raise OSError("forced uncovered tail stratum")
            return json.dumps({"data": {"total": 1600, "diff": _page_rows(page)}}, ensure_ascii=False)

    try:
        market_breadth_source.fetch_market_breadth(
            FakeBase,
            datetime(2026, 8, 7, 14, 30, 0),
            _anchor_indices(),
        )
    except RuntimeError as exc:
        text = str(exc)
    else:
        raise AssertionError("incomplete strata must reject universe extrapolation")

    assert "incomplete stratified breadth coverage" in text
    assert "successful_strata" in text
    assert "failed_strata" in text
    print("PASS missing_tail_stratum_rejected")


def test_breadth_freshness_uses_index_session_anchor_not_wall_clock():
    class FakeBase:
        @staticmethod
        def http_get(url, timeout=0, attempts=0):
            rows = _page_rows(1, page_size=4, total=4)
            return json.dumps({"data": {"total": 4, "diff": rows}}, ensure_ascii=False)

    result = market_breadth_source.fetch_market_breadth(
        FakeBase,
        datetime(2026, 8, 9, 12, 0, 0),
        _anchor_indices(date="2026-08-07", freshness="LAST_SESSION"),
    )
    assert result["collected_at_cst"] == "2026-08-09 12:00:00"
    assert result["market_session_date"] == "2026-08-07"
    assert result["freshness"] == "LAST_SESSION"
    assert result["freshness_basis"] == "INDEX_QUOTE_SESSION_ANCHOR"
    assert result["session_anchor"]["latest_market_time_cst"].startswith("2026-08-07")
    print("PASS freshness_anchored_to_market_session")


def test_breadth_without_session_anchor_is_unknown():
    class FakeBase:
        @staticmethod
        def http_get(url, timeout=0, attempts=0):
            rows = _page_rows(1, page_size=4, total=4)
            return json.dumps({"data": {"total": 4, "diff": rows}}, ensure_ascii=False)

    result = market_breadth_source.fetch_market_breadth(
        FakeBase,
        datetime(2026, 8, 7, 14, 30, 0),
        {},
    )
    assert result["market_session_date"] is None
    assert result["freshness"] == "UNKNOWN"
    assert result["freshness_basis"] == "NO_RELIABLE_SESSION_ANCHOR"
    print("PASS missing_session_anchor_unknown")


def test_sample_unavailable_stays_separate_from_flat():
    records = []
    pcts = [1, 1, 1, 1, 1, 1, -1, -1, 0, None]
    for i, pct in enumerate(pcts):
        records.append(
            {"f13": 1, "f12": str(600000 + i), "f14": f"样本{i}", "f3": pct, "f6": 100000000, "f2": 10, "f15": 10, "f18": 10}
        )
    estimate = market_breadth_source._estimate_stratum(records, 100)
    assert estimate["change_covered_count"] == 90
    assert estimate["unavailable_change_count"] == 10
    assert estimate["up_count"] == 60
    assert estimate["down_count"] == 20
    assert estimate["flat_count"] == 10
    assert estimate["sample_unavailable_change_count"] == 1
    print("PASS sampled_unavailable_separate")


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

    assert data["schema_version"] == 22
    assert data["features"]["market_environment"] == "v1"
    assert data["features"]["breadth_bootstrap"] == "v1"
    assert data["market_environment"]["indices"]["covered_count"] == 6
    assert data["market_environment"]["breadth"]["overall"]["up_count"] == 2800
    assert data["market_environment"]["summary"]
    print("PASS finalize_snapshot_schema22")


def main():
    tests = [
        test_broad_risk_on_market_driver,
        test_broad_market_reference_excludes_style_indices,
        test_sector_driver_high_requires_quality_and_separation,
        test_sector_driver_single_peer_is_low_confidence,
        test_sector_driver_four_peers_is_capped_medium,
        test_idiosyncratic_driver_when_stock_breaks_from_market_and_sector,
        test_stale_indices_are_not_counted_as_usable,
        test_market_record_summary_and_limit_diagnostics,
        test_stratified_sample_uses_neighbor_inside_failed_target_stratum,
        test_missing_tail_stratum_rejects_extrapolation_even_with_large_sample,
        test_breadth_freshness_uses_index_session_anchor_not_wall_clock,
        test_breadth_without_session_anchor_is_unknown,
        test_sample_unavailable_stays_separate_from_flat,
        test_finalize_snapshot_updates_schema_and_feature,
    ]
    for test in tests:
        test()
    print(f"MARKET_ENVIRONMENT_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
