import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import changes_since_previous
import market_calendar
import minute_history
import relative_strength_windows as relative


def _labels(now_text="2026-08-10 10:30:30"):
    now = datetime.fromisoformat(now_text).replace(tzinfo=market_calendar.CST)
    return market_calendar.expected_minute_times(now)["expected_times"]


def _points(labels, base=10.0, step=0.001):
    return [
        {
            "time": label,
            "price": base * (1.0 + step * index),
            "cum_volume": float((index + 1) * 100),
            "cum_amount": float((index + 1) * 100000),
            "delta_volume": 100.0,
            "delta_amount": 100000.0,
        }
        for index, label in enumerate(labels)
    ]


def _base():
    def infer(code):
        prefix = "sh" if str(code).startswith("6") else "sz"
        return ("SH" if prefix == "sh" else "SZ", "", prefix + str(code))

    return SimpleNamespace(infer_identifiers=infer)


def _config():
    return {
        "groups": {
            "game": {
                "label": "game peers",
                "target_code": "002558",
                "member_codes": ["002555", "002517"],
                "active_member_codes": ["002555", "002517"],
            }
        }
    }


def _snapshot(root, now_text="2026-08-10 10:30:30"):
    labels = _labels(now_text)
    observation = {
        "runner_time_cst": now_text,
        "runner_time_utc": "2026-08-10T02:30:30+00:00",
        "run_id": 100,
        "run_attempt": 1,
        "head_sha": "a" * 40,
        "source": "GITHUB_ACTIONS",
    }
    snapshot = {
        "schema_version": 20,
        "runner_time_cst": now_text,
        "runner_time_utc": observation["runner_time_utc"],
        "observation": observation,
        "detail_stocks": {"002558": {"code": "002558", "status": "OK"}},
    }
    document = minute_history.build_document(
        "002558", "2026-08-10", _points(labels, step=0.002), snapshot
    )
    rel = Path("minutes/2026-08-10/002558.json")
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    snapshot["detail_stocks"]["002558"]["minute_history"] = minute_history._locator(
        document, rel, observation
    )
    return snapshot, labels


def _benchmarks(labels):
    return {
        "sz002555": {"date": "20260810", "points": _points(labels, step=0.0010), "error": None},
        "sz002517": {"date": "20260810", "points": _points(labels, step=0.0012), "error": None},
        "sz399006": {"date": "20260810", "points": _points(labels, step=0.0008), "error": None},
        "sh000852": {"date": "20260810", "points": _points(labels, step=0.0006), "error": None},
    }


def test_synchronized_windows_are_deterministically_recomputable():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("MARKET_HISTORY_DIR")
        os.environ["MARKET_HISTORY_DIR"] = tmp
        try:
            snapshot, labels = _snapshot(tmp)
            result = relative.build_relative_strength(
                snapshot, _config(), _benchmarks(labels), _base()
            )["002558"]
        finally:
            if old is None:
                os.environ.pop("MARKET_HISTORY_DIR", None)
            else:
                os.environ["MARKET_HISTORY_DIR"] = old
    assert result["cutoff"] == labels[-1]
    assert result["forming_minute_excluded"] == "1030"
    assert result["metadata"]["freshness"] == "DERIVED_CURRENT"
    assert result["metadata"]["quality"] == "PASS"
    assert result["metadata"]["verification_status"] == "VERIFIED"
    assert result["provenance"]["algorithm"] == "synchronized_window_return_v1"
    assert result["provenance"]["calendar_version"]
    for window in (5, 15, 30):
        value = result["vs_indices"]["chinext"]["windows"][f"{window}m"]
        assert value["window_end"] == result["cutoff"]
        assert value["status"] == "OK"
        assert value["excess_return_percent"] == round(
            value["target_return_percent"] - value["benchmark_return_percent"], 4
        )
    group = result["vs_groups"]["game"]["windows"]["15m"]
    assert group["coverage"]["covered_peer_count"] == 2
    assert group["coverage"]["aggregation_method"] == "MEDIAN_EQUAL_WEIGHT"
    assert group["quality"] == "PASS"


def test_gap_peer_loss_and_session_mismatch_degrade_without_substitution():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("MARKET_HISTORY_DIR")
        os.environ["MARKET_HISTORY_DIR"] = tmp
        try:
            snapshot, labels = _snapshot(tmp)
            raw = _benchmarks(labels)
            missing_label = labels[-8]
            raw["sz002517"]["points"] = [
                point for point in raw["sz002517"]["points"] if point["time"] != missing_label
            ]
            raw["sz399006"]["date"] = "20260807"
            result = relative.build_relative_strength(snapshot, _config(), raw, _base())["002558"]
        finally:
            if old is None:
                os.environ.pop("MARKET_HISTORY_DIR", None)
            else:
                os.environ["MARKET_HISTORY_DIR"] = old
    group = result["vs_groups"]["game"]["windows"]["15m"]
    assert group["status"] == "PARTIAL"
    assert group["coverage"]["covered_peer_codes"] == ["002555"]
    assert group["coverage"]["peer_results"]["002517"]["status"] == "GAPPED"
    index = result["vs_indices"]["chinext"]["windows"]["15m"]
    assert index["status"] == "UNAVAILABLE"
    assert index["benchmark_return_percent"] is None
    assert "SESSION_DATE_MISMATCH" in index["reason_codes"]


def _window_context(covered_codes, excess, state="OUTPERFORMING", coverage=100.0):
    return {
        "session_date": "2026-08-10",
        "vs_groups": {
            "game": {
                "windows": {
                    "15m": {
                        "window_start": "1014",
                        "window_end": "1029",
                        "excess_return_percent": excess,
                        "state": state,
                        "coverage": {
                            "requested_peer_codes": ["002517", "002555"],
                            "covered_peer_codes": covered_codes,
                            "peer_coverage_percent": coverage,
                        },
                    }
                }
            }
        },
        "vs_indices": {},
    }


def test_changes_require_stable_peer_universe():
    before = {"relative_strength_windows": _window_context(["002517"], 0.2, coverage=50.0)}
    recovered = {
        "relative_strength_windows": _window_context(["002517", "002555"], 1.0, coverage=100.0)
    }
    changed = changes_since_previous._relative_window_changes(before, recovered)
    window = changed["vs_groups"]["game"]["windows"]["15m"]
    assert window["excess_return_percent"]["comparable"] is False
    assert window["coverage_percent"]["delta"] == 50.0
    assert "COVERED_PEER_UNIVERSE_CHANGED" in window["quality_flags"]

    stable = {"relative_strength_windows": _window_context(["002517"], 1.0, coverage=50.0)}
    comparable = changes_since_previous._relative_window_changes(before, stable)
    value = comparable["vs_groups"]["game"]["windows"]["15m"]
    assert value["excess_return_percent"]["comparable"] is True
    assert value["excess_return_percent"]["delta"] == 0.8

    reordered = {
        "relative_strength_windows": _window_context(["002517"], 1.0, coverage=50.0)
    }
    reordered_coverage = reordered["relative_strength_windows"]["vs_groups"]["game"][
        "windows"
    ]["15m"]["coverage"]
    reordered_coverage["requested_peer_codes"] = ["002555", "002517"]
    order_only = changes_since_previous._relative_window_changes(before, reordered)
    assert (
        order_only["vs_groups"]["game"]["windows"]["15m"]["excess_return_percent"][
            "comparable"
        ]
        is True
    )


def main():
    tests = [
        test_synchronized_windows_are_deterministically_recomputable,
        test_gap_peer_loss_and_session_mismatch_degrade_without_substitution,
        test_changes_require_stable_peer_universe,
    ]
    for test in tests:
        test()
    print(f"RELATIVE_STRENGTH_WINDOWS_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
