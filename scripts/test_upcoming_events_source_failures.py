import json
import tempfile
from pathlib import Path

import upcoming_events
import upcoming_events_quality


def test_failed_only_source_fails_closed():
    stock = {"events": {"status": "ERROR", "upcoming": []}}
    value = upcoming_events.build_upcoming_events(stock, "2026-08-13")
    assert value["status"] == "FAILED"
    assert value["metadata"]["quality"] == "FAILED"

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(json.dumps({"runner_time_cst": "2026-08-13 06:10:00", "detail_stocks": {"002558": stock}}), encoding="utf-8")
        upcoming_events.finalize_snapshot(path)
        upcoming_events_quality.finalize_snapshot(path)
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        layer = snapshot["detail_stocks"]["002558"]["upcoming_events"]
        assert layer["status"] == "FAILED"
        assert layer["metadata"]["quality"] == "FAILED"
        assert "SOURCE_COMPANY_EVENTS_ERROR" in layer["metadata"]["quality_flags"]
        assert snapshot["upcoming_events_summary"]["status_by_code"] == {"002558": "FAILED"}
        assert snapshot["upcoming_events_summary"]["metadata"]["quality"] == "PARTIAL"


def test_mixed_healthy_and_failed_sources_are_partial():
    stock = {
        "events": {"status": "FAILED", "upcoming": []},
        "ownership_and_capital": {"unlocks": {"status": "OK", "upcoming": []}},
    }
    value = upcoming_events.build_upcoming_events(stock, "2026-08-13")
    assert value["status"] == "PARTIAL"
    assert value["metadata"]["quality"] == "PARTIAL"
    assert value["metadata"]["source_status"]["company_events"] == "FAILED"
    assert value["metadata"]["source_status"]["ownership_unlocks"] == "OK"

    value["status"] = "OK"
    value["metadata"]["quality"] = "PASS"
    quality = upcoming_events_quality._decorate_layer(value, "2026-08-13T12:00:00+08:00")
    assert quality == "PARTIAL"
    assert value["metadata"]["quality"] == "PARTIAL"
    assert "SOURCE_COMPANY_EVENTS_FAILED" in value["metadata"]["quality_flags"]


def main():
    tests = [test_failed_only_source_fails_closed, test_mixed_healthy_and_failed_sources_are_partial]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"UPCOMING_EVENTS_SOURCE_FAILURE_TESTS passed={len(tests)}")
