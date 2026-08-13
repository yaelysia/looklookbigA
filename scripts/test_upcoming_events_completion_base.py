import json
import tempfile
from pathlib import Path

import test_upcoming_events
import test_upcoming_events_calendar
import test_upcoming_events_changes
import upcoming_events_quality


def _layer(status="OK", unverified=0, excluded=0):
    return {
        "status": status,
        "as_of_date": "2026-08-13",
        "next_7d": [],
        "next_30d": [],
        "next_90d": [],
        "later": [],
        "calendar_summary": {
            "event_count": 0,
            "unverified_trading_day_context_count": unverified,
        },
        "metadata": {
            "freshness": "DERIVED_FROM_CURRENT_SNAPSHOT_FACTS",
            "quality": "PASS" if status == "OK" else "PARTIAL",
            "source_status": {
                "company_events": status,
                "ownership_unlocks": "OK",
                "ownership_plans": "OK",
            },
            "excluded_unproven_company_event_count": excluded,
        },
        "provenance": {
            "source_layers": [
                "detail_stocks.<code>.events.upcoming",
                "detail_stocks.<code>.ownership_and_capital.unlocks",
            ]
        },
    }


def test_quality_contract_is_visible_to_global_metadata_collector():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(
            json.dumps(
                {
                    "runner_time_cst": "2026-08-13 06:10:00",
                    "detail_stocks": {"002558": {"upcoming_events": _layer()}},
                    "upcoming_events_summary": {"status": "OK"},
                }
            ),
            encoding="utf-8",
        )
        upcoming_events_quality.finalize_snapshot(path)
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        metadata = snapshot["detail_stocks"]["002558"]["upcoming_events"]["metadata"]
        assert metadata["source"] == "DERIVED"
        assert metadata["source_type"] == "DERIVED"
        assert metadata["source_tier"] == "DERIVED"
        assert metadata["freshness_policy"] == "UPCOMING_EVENTS_CURRENT_SNAPSHOT_FACTS"
        assert metadata["quality"] == "PASS"
        assert metadata["confidence"] == "HIGH"
        assert metadata["fallback_used"] is False
        provenance = snapshot["detail_stocks"]["002558"]["upcoming_events"]["provenance"]
        assert provenance["algorithm"] == "upcoming_events_v1"
        assert provenance["quality_contract"] == "provenance_freshness_quality_v1"
        summary = snapshot["upcoming_events_summary"]
        assert summary["metadata"]["quality"] == "PASS"
        assert summary["quality_by_code"] == {"002558": "PASS"}


def test_unverified_trading_context_degrades_context_not_event_fact():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(
            json.dumps(
                {
                    "runner_time_utc": "2026-08-12T22:10:00Z",
                    "detail_stocks": {
                        "002558": {"upcoming_events": _layer(unverified=1, excluded=2)}
                    },
                    "upcoming_events_summary": {"status": "OK"},
                }
            ),
            encoding="utf-8",
        )
        upcoming_events_quality.finalize_snapshot(path)
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        metadata = snapshot["detail_stocks"]["002558"]["upcoming_events"]["metadata"]
        assert metadata["quality"] == "DEGRADED"
        assert metadata["confidence"] == "MEDIUM"
        assert "TRADING_DAY_CONTEXT_UNVERIFIED" in metadata["quality_flags"]
        assert "UNPROVEN_DATES_EXCLUDED_FAIL_CLOSED" in metadata["quality_flags"]
        assert snapshot["upcoming_events_summary"]["metadata"]["quality"] == "DEGRADED"


def test_partial_source_propagates_fail_closed_quality():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(
            json.dumps(
                {
                    "runner_time_cst": "2026-08-13 06:10:00",
                    "detail_stocks": {
                        "002558": {"upcoming_events": _layer(status="PARTIAL")}
                    },
                    "upcoming_events_summary": {"status": "PARTIAL"},
                }
            ),
            encoding="utf-8",
        )
        upcoming_events_quality.finalize_snapshot(path)
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        metadata = snapshot["detail_stocks"]["002558"]["upcoming_events"]["metadata"]
        assert metadata["quality"] == "PARTIAL"
        assert metadata["confidence"] == "LOW"
        assert "SOURCE_COMPANY_EVENTS_PARTIAL" in metadata["quality_flags"]
        assert snapshot["upcoming_events_summary"]["metadata"]["quality"] == "PARTIAL"


def test_required_gate_has_one_completion_entrypoint():
    workflow = Path(".github/workflows/pre-merge-security-gate.yml").read_text(encoding="utf-8")
    command = "python3 scripts/test_upcoming_events_completion.py"
    assert workflow.count(command) == 1


def main():
    test_upcoming_events.main()
    test_upcoming_events_calendar.main()
    test_upcoming_events_changes.main()
    tests = [
        test_quality_contract_is_visible_to_global_metadata_collector,
        test_unverified_trading_context_degrades_context_not_event_fact,
        test_partial_source_propagates_fail_closed_quality,
        test_required_gate_has_one_completion_entrypoint,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"UPCOMING_EVENTS_COMPLETION_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
