import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import history_continuity
import intraday_metrics
import market_calendar
import minute_history


def _snapshot(runner_time, run_id=100, attempt=1, head_sha=None):
    return {
        "schema_version": 19,
        "runner_time_cst": runner_time,
        "runner_time_utc": "2026-08-10T07:06:00+00:00",
        "observation": {
            "runner_time_cst": runner_time,
            "runner_time_utc": "2026-08-10T07:06:00+00:00",
            "run_id": run_id,
            "run_attempt": attempt,
            "head_sha": head_sha or ("a" * 40),
            "source": "GITHUB_ACTIONS",
        },
        "detail_stocks": {"002558": {"code": "002558"}},
    }


def _points(labels):
    result = []
    for index, label in enumerate(labels, start=1):
        result.append(
            {
                "time": label,
                "price": 10.0 + index / 10000,
                "cum_volume": float(index * 100),
                "cum_amount": float(index * 100000),
                "delta_volume": 100.0,
                "delta_amount": 100000.0,
            }
        )
    return result


def _expected(runner_time):
    now = datetime.fromisoformat(runner_time).replace(tzinfo=market_calendar.CST)
    return market_calendar.expected_minute_times(now, session_date="2026-08-10")["expected_times"]


def _history_tree(root, manifest, document):
    root = Path(root)
    rel = Path(manifest["latest_snapshot"])
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(json.dumps({"runner_time_cst": manifest["latest_runner_time_cst"]}), encoding="utf-8")
    minute_path = root / "minutes/2026-08-10/002558.json"
    minute_path.parent.mkdir(parents=True, exist_ok=True)
    minute_path.write_text(json.dumps(document), encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_complete_closed_session_is_replay_eligible():
    labels = _expected("2026-08-10 15:06:00")
    document = minute_history.build_document(
        "002558",
        "20260810",
        _points(labels),
        _snapshot("2026-08-10 15:06:00"),
    )
    assert len(labels) == 242
    assert document["coverage"]["status"] == "COMPLETE"
    assert document["continuity"]["status"] == "PASS"
    assert document["finality"]["status"] == "FINAL"
    assert document["replay"] == {
        "contract_version": "v1",
        "eligible": True,
        "reason_codes": [],
    }


def test_in_progress_forming_minute_is_excluded_until_final():
    all_labels = _expected("2026-08-10 15:06:00")
    document = minute_history.build_document(
        "002558",
        "2026-08-10",
        _points(all_labels),
        _snapshot("2026-08-10 10:00:00"),
    )
    assert document["coverage"]["observed_count"] < len(all_labels)
    assert document["calendar_expectation"]["forming_minute"] == "0959"
    assert document["finality"]["status"] == "PROVISIONAL"
    assert document["replay"]["eligible"] is False
    assert "SESSION_NOT_COMPLETE" in document["replay"]["reason_codes"]


def test_merge_completes_gaps_but_conflicts_disqualify_replay():
    labels = _expected("2026-08-10 15:06:00")
    first = minute_history.build_document(
        "002558", "2026-08-10", _points(labels[:-1]), _snapshot("2026-08-10 15:06:00", 101)
    )
    second = minute_history.build_document(
        "002558", "2026-08-10", _points(labels), _snapshot("2026-08-10 15:07:00", 102)
    )
    merged = minute_history.merge_documents(first, second)
    assert merged["coverage"]["missing_count"] == 0
    assert merged["replay"]["eligible"] is True
    assert len(merged["observations"]) == 2

    conflicting = json.loads(json.dumps(second))
    conflicting["points"][0]["price"] = 99.0
    conflicted = minute_history.merge_documents(merged, conflicting)
    assert conflicted["conflicts"][0]["time"] == labels[0]
    assert conflicted["replay"]["eligible"] is False
    assert "CONFLICTING_MINUTE_REVISIONS" in conflicted["replay"]["reason_codes"]


def test_snapshot_locator_reader_and_late_writer_union():
    labels = _expected("2026-08-10 15:06:00")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        history_root = base / "history"
        old_root = os.environ.get("MARKET_HISTORY_DIR")
        os.environ["MARKET_HISTORY_DIR"] = str(history_root)
        fake = SimpleNamespace()
        fake.infer_identifiers = lambda code: ("SZ", "0." + code, "sz" + code)
        fake.parse_minutes = lambda rows: list(rows)
        fake.tencent_minutes = lambda tcode: ("20260810", _points(labels))
        minute_history.CAPTURES.clear()
        minute_history.install(fake)
        fake.tencent_minutes("sz002558")
        snapshot_path = base / "snapshot.json"
        snapshot_path.write_text(json.dumps(_snapshot("2026-08-10 15:06:00")), encoding="utf-8")
        try:
            minute_history.finalize_snapshot(snapshot_path)
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            loaded = minute_history.load_from_snapshot(
                snapshot, "002558", history_root, require_replay_eligible=True
            )
            assert loaded["coverage"]["observed_count"] == 242
            assert snapshot["detail_stocks"]["002558"]["minute_history"]["path"] == (
                "minutes/2026-08-10/002558.json"
            )
            escaped = json.loads(json.dumps(snapshot))
            escaped["detail_stocks"]["002558"]["minute_history"]["path"] = "snapshots/current.json"
            try:
                minute_history.load_from_snapshot(escaped, "002558", history_root)
            except ValueError as exc:
                assert "minutes namespace" in str(exc)
            else:
                raise AssertionError("snapshot locator outside minutes namespace must be rejected")

            current = base / "current"
            incoming = base / "incoming"
            current_doc = json.loads(json.dumps(loaded))
            current_doc["points"] = current_doc["points"][1:]
            minute_history._recompute(current_doc)
            current_manifest = {
                "latest_snapshot": "snapshots/current.json",
                "latest_runner_time_cst": "2026-08-10 15:07:00",
                "latest_run_id": 201,
                "latest_run_attempt": 1,
                "latest_head_sha": "c" * 40,
            }
            incoming_manifest = {
                "latest_snapshot": "snapshots/incoming.json",
                "latest_runner_time_cst": "2026-08-10 15:06:00",
                "latest_run_id": 200,
                "latest_run_attempt": 1,
                "latest_head_sha": "b" * 40,
            }
            _history_tree(current, current_manifest, current_doc)
            _history_tree(incoming, incoming_manifest, loaded)
            assert history_continuity.persist_if_newer(
                current,
                incoming,
                expected_run_id=200,
                expected_run_attempt=1,
                expected_head_sha="b" * 40,
            ) is True
            union = minute_history.load_day("002558", "2026-08-10", current)
            assert union["coverage"]["missing_count"] == 0
            assert history_continuity.validate_history_tree(current)["latest_run_id"] == 201
        finally:
            minute_history.CAPTURES.clear()
            if old_root is None:
                os.environ.pop("MARKET_HISTORY_DIR", None)
            else:
                os.environ["MARKET_HISTORY_DIR"] = old_root


def test_intraday_and_legacy_minutes_use_calendar_trimmed_canonical_points():
    labels = _expected("2026-08-10 15:06:00")
    canonical_points = _points(labels)
    last = canonical_points[-1]
    extras = [
        {
            "time": f"15{minute:02d}",
            "price": last["price"],
            "cum_volume": last["cum_volume"],
            "cum_amount": last["cum_amount"],
            "delta_volume": 0.0,
            "delta_amount": 0.0,
        }
        for minute in range(1, 31)
    ]
    provider_points = canonical_points + extras
    quote = {"latest": last["price"], "average": 10.0, "high": 11.0, "low": 9.0}
    expected_intraday = intraday_metrics.build_intraday_metrics(quote, canonical_points)
    raw_intraday = intraday_metrics.build_intraday_metrics(quote, provider_points)
    assert raw_intraday["minute_count"] == 272
    assert raw_intraday["minute_last_time"] == "1530"
    assert raw_intraday["trend_15m_percent"] == 0.0
    assert raw_intraday["volume_spike_ratio_1m"] is None
    assert expected_intraday["volume_spike_ratio_1m"] == 1.0

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        history_root = base / "history"
        old_root = os.environ.get("MARKET_HISTORY_DIR")
        os.environ["MARKET_HISTORY_DIR"] = str(history_root)
        fake = SimpleNamespace()
        fake.infer_identifiers = lambda code: ("SZ", "0." + code, "sz" + code)
        fake.parse_minutes = lambda rows: list(rows)
        fake.tencent_minutes = lambda tcode: ("20260810", provider_points)
        minute_history.CAPTURES.clear()
        minute_history.install(fake)
        fake.tencent_minutes("sz002558")

        snapshot = _snapshot("2026-08-10 15:06:00")
        item = snapshot["detail_stocks"]["002558"]
        item["quote"] = quote
        item["minutes"] = {
            "source": "Tencent",
            "date": "20260810",
            "freshness": "CURRENT_SESSION",
            "count": len(provider_points),
            "last_time": "1530",
            "last_price": last["price"],
            "market_time_cst": "2026-08-10 15:30:00",
            "lag_seconds": 0,
            "first_10": provider_points[:10],
            "last_15": provider_points[-15:],
        }
        raw_intraday["current_price_valid"] = True
        raw_intraday["current_price_source_class"] = "SESSION_QUOTE"
        raw_intraday["current_price_provider"] = "Eastmoney"
        raw_intraday["current_price_guard"] = {
            "status": "OK",
            "minute_freshness": "CURRENT_SESSION",
            "minute_lag_seconds": 0,
            "hard_violations": [],
        }
        item["intraday"] = raw_intraday
        snapshot_path = base / "snapshot.json"
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

        try:
            minute_history.finalize_snapshot(snapshot_path)
            intraday_metrics.finalize_snapshot(snapshot_path)
            result = json.loads(snapshot_path.read_text(encoding="utf-8"))
            item = result["detail_stocks"]["002558"]
            minutes = item["minutes"]
            intraday = item["intraday"]

            assert result["schema_version"] == 20
            assert result["features"]["intraday_structure_metrics"] == "v2"
            assert minutes["count"] == 242
            assert minutes["last_time"] == "1500"
            assert minutes["market_time_cst"] == "2026-08-10 15:00:00"
            assert minutes["lag_seconds"] == 360
            assert minutes["provider_observation"]["count"] == 272
            assert minutes["provider_observation"]["last_time"] == "1530"
            assert minutes["normalization"]["authority"] == "minute_history"
            assert minutes["normalization"]["unexpected_provider_times"][-1] == "1530"

            assert intraday["minute_count"] == 242
            assert intraday["minute_last_time"] == "1500"
            for field in (
                "trend_5m_percent",
                "trend_15m_percent",
                "trend_30m_percent",
                "reference_time",
                "volume_spike_ratio_1m",
                "amount_spike_ratio_1m",
                "volume_strength_ratio_5m",
                "amount_strength_ratio_5m",
                "structure",
                "bias",
                "last_swing_highs",
                "last_swing_lows",
            ):
                assert intraday[field] == expected_intraday[field]
            assert intraday["canonical_minute_history"]["status"] == "REPLAY_READY"
            assert intraday["current_price_guard"]["minute_lag_seconds"] == 360
        finally:
            minute_history.CAPTURES.clear()
            if old_root is None:
                os.environ.pop("MARKET_HISTORY_DIR", None)
            else:
                os.environ["MARKET_HISTORY_DIR"] = old_root


def test_intraday_does_not_fallback_to_raw_minutes_without_canonical_history():
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = _snapshot("2026-08-10 15:06:00")
        item = snapshot["detail_stocks"]["002558"]
        item["minutes"] = {"count": 267, "last_time": "1530"}
        item["intraday"] = {"minute_count": 267, "minute_last_time": "1530"}
        snapshot_path = Path(tmp) / "snapshot.json"
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

        try:
            intraday_metrics.finalize_snapshot(snapshot_path)
        except RuntimeError as exc:
            assert "canonical minute history is unavailable" in str(exc)
        else:
            raise AssertionError("raw provider minutes must not be used as fallback")


def main():
    tests = [
        test_complete_closed_session_is_replay_eligible,
        test_in_progress_forming_minute_is_excluded_until_final,
        test_merge_completes_gaps_but_conflicts_disqualify_replay,
        test_snapshot_locator_reader_and_late_writer_union,
        test_intraday_and_legacy_minutes_use_calendar_trimmed_canonical_points,
        test_intraday_does_not_fallback_to_raw_minutes_without_canonical_history,
    ]
    for test in tests:
        test()
    print(f"MINUTE_HISTORY_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
