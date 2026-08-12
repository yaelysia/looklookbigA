import json
import os
import tempfile
from pathlib import Path

import changes_since_previous as changes
import history_store


def _group(target_pct, peer_pcts, breadth=0.0, status="OK"):
    members = []
    for idx, pct in enumerate(peer_pcts):
        members.append(
            {
                "code": f"3000{idx:02d}",
                "name": f"peer{idx}",
                "change_percent": pct,
                "available": True,
            }
        )
    return {
        "status": status,
        "target": {"code": "002558", "change_percent": target_pct},
        "requested_member_count": len(members),
        "covered_member_count": len(members),
        "coverage_percent": 100,
        "mean_change_percent": sum(peer_pcts) / len(peer_pcts) if peer_pcts else None,
        "median_change_percent": sorted(peer_pcts)[len(peer_pcts) // 2] if peer_pcts else None,
        "breadth_score_percent": breadth,
        "target_vs_peer_mean_percent": target_pct - (sum(peer_pcts) / len(peer_pcts)) if peer_pcts else None,
        "members": members,
    }


def _event(event_id, title, importance="MEDIUM", status="OPEN", facts=None):
    return {
        "event_id": event_id,
        "event_type": "BUYBACK",
        "title": title,
        "published_at": "2026-08-07T09:00:00+08:00",
        "importance": importance,
        "status": status,
        "facts": facts or {},
        "source_url": f"https://example.invalid/{event_id}",
    }


def _snapshot(
    timestamp,
    latest=10.0,
    pct=1.0,
    amount=100.0,
    trend15=0.1,
    bias="RANGE",
    vs_market=0.2,
    vs_group=0.1,
    driver="MARKET",
    group_target=1.0,
    group_peers=(2.0, 1.5, 0.5),
    group_breadth=0.0,
    regime="BALANCED",
    style="BALANCED",
    breadth_score=0.0,
    breadth_amount=10000.0,
    events=None,
    schema=10,
):
    date = timestamp[:10]
    return {
        "schema_version": schema,
        "runner_time_cst": timestamp,
        "runner_time_utc": None,
        "observation": {
            "runner_time_cst": timestamp,
            "runner_time_utc": None,
            "run_id": int(timestamp[11:13] + timestamp[14:16] + timestamp[17:19]),
            "run_attempt": 1,
            "head_sha": "a" * 40,
            "source": "GITHUB_ACTIONS",
        },
        "features": {},
        "detail_stocks": {
            "002558": {
                "code": "002558",
                "status": "OK",
                "quote": {
                    "latest": latest,
                    "change_percent": pct,
                    "high": latest + 0.5,
                    "low": latest - 0.5,
                    "amplitude_percent": 5.0,
                    "amount_1e8": amount,
                    "market_time_cst": f"{date} {timestamp[11:19]}",
                    "freshness": "LIVE",
                },
                "intraday": {
                    "trend_5m_percent": trend15 / 2,
                    "trend_15m_percent": trend15,
                    "trend_30m_percent": trend15 * 1.5,
                    "price_vs_vwap_percent": trend15,
                    "day_range_position_percent": 50,
                    "volume_strength_ratio_5m": 1.0,
                    "amount_strength_ratio_5m": 1.0,
                    "bias": bias,
                    "structure": "RANGE_OR_FLAT" if bias == "RANGE" else "HIGHER_HIGH_HIGHER_LOW",
                    "above_vwap": trend15 >= 0,
                },
                "events": {
                    "status": "OK",
                    "recent": list(events or []),
                    "latest": (list(events or [None])[0] if events else None),
                    "upcoming": [],
                } if events is not None else None,
            }
        },
        "groups": {
            "game": _group(group_target, list(group_peers), group_breadth),
        },
        "indices": {
            "上证指数": {"status": "OK", "quote": {"change_percent": 0.3, "freshness": "LIVE"}},
            "沪深300": {"status": "OK", "quote": {"change_percent": 0.2, "freshness": "LIVE"}},
        },
        "market_environment": {
            "status": "OK",
            "regime": {"status": regime},
            "style": {"status": style, "spreads": {"small_vs_large_percent": 0.2}},
            "breadth": {
                "status": "OK",
                "estimated": False,
                "market_session_date": date,
                "source": "fixture",
                "overall": {
                    "up_count": 3000,
                    "down_count": 2000,
                    "flat_count": 100,
                    "unavailable_change_count": 0,
                    "up_ratio_percent": 58.8,
                    "down_ratio_percent": 39.2,
                    "breadth_score_percent": breadth_score,
                    "amount_1e8": breadth_amount,
                },
            },
            "targets": {
                "002558": {
                    "relative_strength": {
                        "vs_market_percent": vs_market,
                        "vs_group_mean_percent": vs_group,
                        "relative_to_market": "INLINE" if abs(vs_market) < 0.5 else "OUTPERFORM",
                        "relative_to_group": "INLINE" if abs(vs_group) < 0.5 else "OUTPERFORM",
                    },
                    "driver_attribution": {"primary_driver": driver},
                }
            },
        },
        "data_quality": {"overall": "PASS"},
    }


def test_no_baseline_does_not_invent_changes():
    current = _snapshot("2026-08-07 10:30:00")
    result = changes.build_changes(None, current, None)
    assert result["status"] == "NO_BASELINE"
    assert result["market"] is None
    assert result["stocks"] == {}
    assert result["groups"] == {}
    assert result["summary"]["significant_changes"] == 0
    assert result["baseline"]["quality_flags"] == ["NO_VALID_PREVIOUS_SNAPSHOT"]
    print("PASS no_baseline")


def test_stock_group_market_changes_keep_before_after_and_significance():
    previous = _snapshot(
        "2026-08-07 10:15:00",
        latest=10.0,
        pct=0.5,
        amount=100,
        trend15=-0.2,
        bias="RANGE",
        vs_market=-0.2,
        vs_group=-0.8,
        group_target=0.5,
        group_peers=(2.0, 1.5, 1.0),
        group_breadth=-20,
        regime="BALANCED",
        style="LARGE_CAP_LEADING",
        breadth_score=-10,
        breadth_amount=10000,
    )
    current = _snapshot(
        "2026-08-07 10:30:00",
        latest=10.3,
        pct=2.2,
        amount=160,
        trend15=1.0,
        bias="UPTREND",
        vs_market=1.1,
        vs_group=1.0,
        driver="IDIOSYNCRATIC",
        group_target=2.2,
        group_peers=(1.5, 1.0, 0.5),
        group_breadth=40,
        regime="BROAD_RISK_ON",
        style="SMALL_CAP_LEADING",
        breadth_score=30,
        breadth_amount=12000,
    )
    result = changes.build_changes(previous, current, "snapshots/prev.json")
    assert result["status"] == "OK"
    assert result["baseline"]["interval_seconds"] == 900.0
    assert result["baseline"]["previous_observation"]["run_id"] == 101500
    assert result["baseline"]["current_observation"]["run_id"] == 103000

    stock = result["stocks"]["002558"]
    assert stock["price_change"]["latest"]["before"] == 10.0
    assert stock["price_change"]["latest"]["after"] == 10.3
    assert stock["price_change"]["change_percent"]["delta"] == 1.7
    assert stock["turnover_change"]["incremental_amount_1e8"] == 60.0
    assert stock["turnover_change"]["incremental_amount_per_minute_1e8"] == 4.0
    assert stock["intraday_change"]["states"]["bias"]["before"] == "RANGE"
    assert stock["intraday_change"]["states"]["bias"]["after"] == "UPTREND"
    assert stock["relative_strength_change"]["vs_group_mean_percent"]["delta"] == 1.8
    assert stock["strength_direction"] == "STRONGER"
    assert stock["significance"] == "SIGNIFICANT"

    group = result["groups"]["game"]
    assert group["target_rank"]["before"] == 4
    assert group["target_rank"]["after"] == 1
    assert group["target_rank"]["rank_improvement"] == 3
    assert group["metrics"]["breadth_score_percent"]["before"] == -20.0
    assert group["metrics"]["breadth_score_percent"]["after"] == 40.0

    market = result["market"]
    assert market["regime"]["before"] == "BALANCED"
    assert market["regime"]["after"] == "BROAD_RISK_ON"
    assert market["breadth"]["ratios"]["breadth_score_percent"]["delta"] == 40.0
    assert market["turnover"]["amount_1e8"]["delta"] == 2000.0
    assert result["summary"]["stronger_stocks"] == ["002558"]
    print("PASS stock_group_market_changes")


def test_cross_session_cumulative_turnover_is_not_compared():
    previous = _snapshot("2026-08-06 14:55:00", amount=500, breadth_amount=18000)
    current = _snapshot("2026-08-07 09:45:00", amount=80, breadth_amount=4000)
    result = changes.build_changes(previous, current, "snapshots/prev.json")
    stock_turnover = result["stocks"]["002558"]["turnover_change"]
    assert stock_turnover["same_market_session"] is False
    assert stock_turnover["amount_1e8"]["comparable"] is False
    assert stock_turnover["amount_1e8"]["delta"] is None
    assert "MARKET_SESSION_RESET" in stock_turnover["quality_flags"]
    assert result["market"]["turnover"]["amount_1e8"]["comparable"] is False
    assert result["market"]["turnover"]["amount_1e8"]["delta"] is None
    print("PASS cross_session_turnover_not_compared")


def test_legacy_baseline_is_partial_but_available_fields_compare():
    previous = _snapshot("2026-08-07 10:15:00", latest=10.0, schema=8)
    previous.pop("market_environment")
    previous.pop("data_quality")
    previous.pop("observation")
    current = _snapshot("2026-08-07 10:30:00", latest=10.2)
    result = changes.build_changes(previous, current, "snapshots/legacy.json")
    assert result["status"] == "PARTIAL"
    assert "BASELINE_MISSING_MARKET_ENVIRONMENT" in result["baseline"]["quality_flags"]
    assert "BASELINE_PREDATES_DATA_PROVENANCE" in result["baseline"]["quality_flags"]
    assert "BASELINE_PREDATES_OBSERVATION_IDENTITY" in result["baseline"]["quality_flags"]
    assert result["stocks"]["002558"]["price_change"]["latest"]["delta"] == 0.2
    assert result["market"]["available"] is False
    print("PASS legacy_baseline_partial")


def test_event_changes_are_id_based_and_missing_does_not_mean_closed():
    previous = _snapshot(
        "2026-08-07 10:15:00",
        events=[
            _event("evt-1", "回购方案", facts={"progress": "PLAN"}),
            _event("evt-2", "另一事件", status="OPEN"),
        ],
    )
    current = _snapshot(
        "2026-08-07 10:30:00",
        events=[
            _event("evt-1", "回购方案", facts={"progress": "IMPLEMENTING"}),
            _event("evt-2", "另一事件", status="COMPLETED"),
            _event("evt-3", "重大回购进展", importance="HIGH"),
        ],
    )
    result = changes.build_changes(previous, current, "snapshots/prev.json")
    events = result["events"]
    assert [x["event_id"] for x in events["new"]] == ["evt-3"]
    assert {x["event_id"] for x in events["updated"]} == {"evt-1", "evt-2"}
    assert [x["event_id"] for x in events["closed"]] == ["evt-2"]
    assert events["significance"] == "SIGNIFICANT"
    assert result["summary"]["new_events"] == 1

    current_missing_evt2 = _snapshot(
        "2026-08-07 10:30:00",
        events=[_event("evt-1", "回购方案", facts={"progress": "PLAN"})],
    )
    missing_result = changes.build_changes(previous, current_missing_evt2, "snapshots/prev.json")
    assert missing_result["events"]["closed"] == []
    print("PASS event_changes")


def test_history_manifest_does_not_advance_before_final_archive():
    old_root = os.environ.get("MARKET_HISTORY_DIR")
    old_run = os.environ.get("GITHUB_RUN_ID")
    old_attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_HISTORY_DIR"] = tmp
            os.environ["GITHUB_RUN_ID"] = "200"
            os.environ["GITHUB_RUN_ATTEMPT"] = "1"
            root = Path(tmp)
            previous_rel = "snapshots/2026-08-07/101500_run100_a1.json"
            previous = _snapshot("2026-08-07 10:15:00")
            previous_path = root / previous_rel
            previous_path.parent.mkdir(parents=True, exist_ok=True)
            previous_path.write_text(json.dumps(previous, ensure_ascii=False), encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "latest_snapshot": previous_rel,
                "latest_runner_time_cst": "2026-08-07 10:15:00",
            }
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            current_path = root / "current.json"
            current = _snapshot("2026-08-07 10:30:00")
            current_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
            history_store.CACHE_META.clear()
            history_store.finalize_snapshot(current_path)

            prepared = json.loads(current_path.read_text(encoding="utf-8"))
            assert prepared["history"]["previous_snapshot_path"] == previous_rel
            assert prepared["history"]["archive_path"] is None
            manifest_after_prepare = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            assert manifest_after_prepare["latest_snapshot"] == previous_rel
            loaded, loaded_rel = history_store.load_previous_snapshot(prepared)
            assert loaded_rel == previous_rel
            assert loaded["runner_time_cst"] == "2026-08-07 10:15:00"

            prepared["schema_version"] = 11
            prepared["data_quality"] = {"overall": "PASS"}
            prepared["changes_since_previous"] = {"status": "OK"}
            current_path.write_text(json.dumps(prepared, ensure_ascii=False), encoding="utf-8")
            history_store.archive_final_snapshot(current_path)

            final_data = json.loads(current_path.read_text(encoding="utf-8"))
            new_rel = final_data["history"]["archive_path"]
            assert new_rel != previous_rel
            assert (root / new_rel).is_file()
            manifest_final = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            assert manifest_final["latest_snapshot"] == new_rel
            archived = json.loads((root / new_rel).read_text(encoding="utf-8"))
            assert archived["schema_version"] == 11
            assert archived["data_quality"]["overall"] == "PASS"
            assert "changes_since_previous" not in archived
    finally:
        if old_root is None:
            os.environ.pop("MARKET_HISTORY_DIR", None)
        else:
            os.environ["MARKET_HISTORY_DIR"] = old_root
        if old_run is None:
            os.environ.pop("GITHUB_RUN_ID", None)
        else:
            os.environ["GITHUB_RUN_ID"] = old_run
        if old_attempt is None:
            os.environ.pop("GITHUB_RUN_ATTEMPT", None)
        else:
            os.environ["GITHUB_RUN_ATTEMPT"] = old_attempt
    print("PASS history_final_archive_transaction")


def main():
    tests = [
        test_no_baseline_does_not_invent_changes,
        test_stock_group_market_changes_keep_before_after_and_significance,
        test_cross_session_cumulative_turnover_is_not_compared,
        test_legacy_baseline_is_partial_but_available_fields_compare,
        test_event_changes_are_id_based_and_missing_does_not_mean_closed,
        test_history_manifest_does_not_advance_before_final_archive,
    ]
    for test in tests:
        test()
    print(f"CHANGES_SINCE_PREVIOUS_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
