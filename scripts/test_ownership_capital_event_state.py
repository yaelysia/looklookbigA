import json
import tempfile
from pathlib import Path

import ownership_capital_event_state as state


def _coverage():
    return {
        "covered_start_date": "2026-04-01",
        "coverage_days": 133,
        "coverage_complete": True,
        "target_history_days": 90,
        "sufficient_for_persistent_state": True,
        "source": "CNINFO_EVENT_CACHE",
    }


def _event(event_id, event_type, published_at, *, status="OPEN", effective_date=None, facts=None, importance="MEDIUM"):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "published_at": published_at,
        "effective_date": effective_date,
        "status": status,
        "title": f"{event_type}-{event_id}",
        "source_tier": "OFFICIAL",
        "source_document_id": event_id,
        "source_url": f"https://static.cninfo.com.cn/{event_id}.PDF",
        "importance": importance,
        "facts": facts or {},
        "related_event_id": None,
        "supersedes_event_id": None,
    }


def test_open_plan_is_not_promoted_to_active_window():
    events = [
        _event(
            "dec-1",
            "HOLDER_DECREASE",
            "2026-08-10T08:00:00+08:00",
            facts={"share_percentages": [1.0], "extraction_scope": "ORIGINAL_PDF_TEXT"},
        )
    ]
    value = state.normalize_plans(events, _coverage(), "2026-08-12", "2026-08-12T14:00:00+08:00")
    plan = value["holder_decrease_plans"]["current"]
    assert plan["status"] == "OPEN"
    assert plan["active_execution_window"] == "UNKNOWN"
    assert "EXECUTION_WINDOW_NOT_EXPLICIT" in plan["quality_flags"]
    assert value["holder_decrease_plans"]["confirmed_active"] == []
    assert value["holder_decrease_plans"]["nonterminal_unknown_window"][0]["event_id"] == "dec-1"


def test_buyback_explicit_progress_and_remaining_range_are_fail_closed():
    events = [
        _event(
            "buy-2",
            "BUYBACK",
            "2026-08-11T08:00:00+08:00",
            facts={
                "amount_min_yuan": 100_000_000,
                "amount_max_yuan": 200_000_000,
                "completed_amount_yuan": 40_000_000,
                "progress": "IN_PROGRESS",
                "extraction_scope": "ORIGINAL_PDF_TEXT",
                "document_extraction": {"status": "OK"},
            },
        ),
        _event(
            "buy-1",
            "BUYBACK",
            "2026-06-01T08:00:00+08:00",
            status="COMPLETED",
            facts={"amount_min_yuan": 50_000_000, "amount_max_yuan": 50_000_000, "progress": "COMPLETED"},
        ),
    ]
    value = state.normalize_plans(events, _coverage(), "2026-08-12", "2026-08-12T14:00:00+08:00")
    current = value["buybacks"]["current"]
    assert current["active_execution_window"] == "ACTIVE"
    assert current["remaining_amount_min_yuan"] == 60_000_000.0
    assert current["remaining_amount_max_yuan"] == 160_000_000.0
    assert value["buybacks"]["confirmed_active"][0]["event_id"] == "buy-2"
    assert value["buybacks"]["history"][1]["active_execution_window"] == "INACTIVE"


def test_unlock_windows_use_explicit_shares_and_keep_unscoped_percentages_unassigned():
    events = [
        _event(
            "unlock-7",
            "UNLOCK",
            "2026-08-01T08:00:00+08:00",
            effective_date="2026-08-18",
            facts={"unlock_shares": 100, "unlock_percentages": [12.3]},
        ),
        _event(
            "unlock-30",
            "UNLOCK",
            "2026-08-01T08:00:00+08:00",
            effective_date="2026-09-05",
            facts={"unlock_percentages": [9.9]},
        ),
        _event(
            "unlock-old",
            "UNLOCK",
            "2026-06-01T08:00:00+08:00",
            effective_date="2026-07-01",
            status="COMPLETED",
            facts={"unlock_shares": 50},
            importance="HIGH",
        ),
    ]
    share = {"values": {"total_shares": 1000, "float_shares": 500, "restricted_shares": 300}}
    value = state.normalize_unlocks(events, _coverage(), "2026-08-12", "2026-08-12T14:00:00+08:00", share)
    assert value["current_restricted_shares"] == 300.0
    assert [item["event_id"] for item in value["upcoming_windows"]["7d"]] == ["unlock-7"]
    assert [item["event_id"] for item in value["upcoming_windows"]["30d"]] == ["unlock-7", "unlock-30"]
    first = value["upcoming_windows"]["7d"][0]
    assert first["unlock_ratio_total_percent"] == 10.0
    assert first["unlock_ratio_float_percent"] == 20.0
    second = value["upcoming_windows"]["30d"][1]
    assert second["unlock_ratio_total_percent"] is None
    assert second["provider_unscoped_percentages"] == [9.9]
    assert "UNLOCK_SHARE_COUNT_UNAVAILABLE" in second["quality_flags"]
    assert value["high_importance_history"][0]["event_id"] == "unlock-old"


def test_extend_snapshot_reads_event_cache_and_fast_is_deferred_without_provider_calls():
    old_path = state.company_events._event_cache_path
    old_read = state.company_events._read_json
    try:
        events = [
            _event(
                "buy-live",
                "BUYBACK",
                "2026-08-10T08:00:00+08:00",
                facts={"progress": "IN_PROGRESS", "amount_min_yuan": 100, "amount_max_yuan": 200},
            )
        ]
        state.company_events._event_cache_path = lambda code: Path(f"/virtual/{code}.json")
        state.company_events._read_json = lambda path: {
            "code": "002558",
            "source": "CNINFO",
            "covered_start_date": "2026-04-01",
            "query_status": "OK",
            "events": events,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            payload = {
                "runner_time_cst": "2026-08-12T14:00:00+08:00",
                "detail_stocks": {
                    "002558": {
                        "events": {"cache": {"coverage_complete": True, "covered_start_date": "2026-04-01"}},
                        "ownership_and_capital": {
                            "share_structure": {"status": "OK", "values": {"total_shares": 1000, "float_shares": 500, "restricted_shares": 300}},
                            "controllers": {"status": "OK"},
                            "top_holders": {"status": "OK"},
                            "institutional_holdings": {"status": "OK"},
                            "shareholder_count": {"status": "OK"},
                        },
                    }
                },
                "ownership_and_capital_summary": {"implemented_sections": ["share_structure", "controllers", "top_holders", "institutional_holdings", "shareholder_count"]},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            state.extend_snapshot(path, object(), "FULL")
            result = json.loads(path.read_text(encoding="utf-8"))
            context = result["detail_stocks"]["002558"]["ownership_and_capital"]
            assert context["buyback_and_holder_plans"]["buybacks"]["current"]["event_id"] == "buy-live"
            assert context["unlocks"]["current_restricted_shares"] == 300.0
            assert result["ownership_and_capital_summary"]["implemented_sections"][-2:] == ["buyback_and_holder_plans", "unlocks"]

            state.company_events._event_cache_path = lambda code: (_ for _ in ()).throw(AssertionError("FAST must not read event cache"))
            fast = Path(tmp) / "fast.json"
            fast.write_text(json.dumps(payload), encoding="utf-8")
            state.extend_snapshot(fast, object(), "INTRADAY_FAST")
            fast_context = json.loads(fast.read_text(encoding="utf-8"))["detail_stocks"]["002558"]["ownership_and_capital"]
            assert fast_context["buyback_and_holder_plans"]["status"] == "DEFERRED"
            assert fast_context["unlocks"]["status"] == "DEFERRED"
    finally:
        state.company_events._event_cache_path = old_path
        state.company_events._read_json = old_read


def main():
    tests = [
        test_open_plan_is_not_promoted_to_active_window,
        test_buyback_explicit_progress_and_remaining_range_are_fail_closed,
        test_unlock_windows_use_explicit_shares_and_keep_unscoped_percentages_unassigned,
        test_extend_snapshot_reads_event_cache_and_fast_is_deferred_without_provider_calls,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OWNERSHIP_EVENT_STATE_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
