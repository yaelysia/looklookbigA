import json
import tempfile
from pathlib import Path

import market_environment


def _snapshot(index_values, group_mean=1.0, group_breadth=50.0, target_pct=1.8):
    names = ["上证指数", "深证成指", "创业板指"]
    indices = {}
    for name, pct in zip(names, index_values):
        indices[name] = {
            "status": "OK",
            "quote": {
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
                "target": {"code": "002558", "name": "巨人网络", "change_percent": target_pct},
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
            "002558": {
                "quote": {
                    "name": "巨人网络",
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


def test_bullish_growth_environment():
    env = market_environment.build_market_environment(_snapshot([0.8, 1.5, 2.0], target_pct=2.2))
    assert env["status"] == "OK"
    assert env["market_bias"] == "STRONG_BULLISH"
    assert env["style"]["status"] == "GROWTH_LEADING"
    assert env["confidence"] == "HIGH"
    target = env["targets"]["002558"]
    assert target["relative_to_market"] == "OUTPERFORM"
    assert target["relative_to_group"] == "OUTPERFORM"
    assert target["intraday_bias"] == "UPTREND"
    assert "市场明显偏强" in env["summary"]
    print("PASS bullish_growth_environment")


def test_bearish_growth_lagging_environment():
    env = market_environment.build_market_environment(
        _snapshot([-0.4, -1.1, -1.5], group_mean=-1.0, group_breadth=-100.0, target_pct=-1.8)
    )
    assert env["market_bias"] == "STRONG_BEARISH"
    assert env["style"]["status"] == "GROWTH_LAGGING"
    assert env["groups"]["game_sector"]["bias"] == "STRONG_BEARISH"
    assert env["targets"]["002558"]["relative_to_market"] == "UNDERPERFORM"
    print("PASS bearish_growth_lagging_environment")


def test_stale_indices_are_not_counted_as_usable():
    snapshot = _snapshot([0.8, 1.0, 1.2])
    snapshot["indices"]["创业板指"]["quote"]["freshness"] = "STALE"
    env = market_environment.build_market_environment(snapshot)
    assert env["indices"]["covered_count"] == 2
    assert env["indices"]["status"] == "PARTIAL"
    assert env["confidence"] == "MEDIUM"
    print("PASS stale_index_excluded")


def test_finalize_snapshot_updates_schema_and_feature():
    snapshot = _snapshot([0.2, 0.3, 0.4])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        market_environment.finalize_snapshot(path)
        data = json.loads(path.read_text(encoding="utf-8"))

    assert data["schema_version"] == 9
    assert data["features"]["market_environment"] == "v1"
    assert data["market_environment"]["indices"]["covered_count"] == 3
    assert data["market_environment"]["summary"]
    print("PASS finalize_snapshot_schema9")


def main():
    tests = [
        test_bullish_growth_environment,
        test_bearish_growth_lagging_environment,
        test_stale_indices_are_not_counted_as_usable,
        test_finalize_snapshot_updates_schema_and_feature,
    ]
    for test in tests:
        test()
    print(f"MARKET_ENVIRONMENT_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
