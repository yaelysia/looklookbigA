import upcoming_events_changes as changes


def event(source_id, date, bucket, importance="MEDIUM", status="UPCOMING"):
    return bucket, {
        "event_id": f"upcoming:{source_id}:EARNINGS_RELEASE:{date}",
        "source_event_id": source_id,
        "event_type": "EARNINGS_RELEASE",
        "title": "2026年第三季度报告披露",
        "event_date": date,
        "date_end": None,
        "date_certainty": "CONFIRMED_DATE",
        "importance": importance,
        "status": status,
    }


def stock(*events, status="OK", source_status=None):
    layer = {"status": status, "next_7d": [], "next_30d": [], "next_90d": [], "later": []}
    for bucket, value in events:
        layer[bucket].append(value)
    result = {"upcoming_events": layer}
    if source_status:
        result["events"] = {"status": "OK", "recent": [{"event_id": "evt1", "status": source_status}]}
    return result


def test_new_and_date_change_are_semantic_not_countdown_noise():
    before = stock(event("evt1", "2026-10-20", "next_90d"))
    after = stock(
        event("evt1", "2026-10-25", "next_90d"),
        event("evt2", "2026-11-10", "later", importance="HIGH"),
    )
    value = changes.build_stock_changes(before, after)
    assert len(value["new"]) == 1
    assert value["new"][0]["source_event_id"] == "evt2"
    assert len(value["date_changed"]) == 1
    assert value["date_changed"][0]["before_event_date"] == "2026-10-20"
    assert value["date_changed"][0]["after_event_date"] == "2026-10-25"
    assert value["window_transitions"] == []
    assert value["significance"] == "SIGNIFICANT"


def test_only_inward_30d_and_7d_threshold_crossings_are_emitted():
    before = stock(event("evt1", "2026-09-20", "next_90d"))
    middle = stock(event("evt1", "2026-09-20", "next_30d"))
    near = stock(event("evt1", "2026-09-20", "next_7d"))
    first = changes.build_stock_changes(before, middle)
    assert [x["kind"] for x in first["window_transitions"]] == ["ENTERED_30D_WINDOW"]
    second = changes.build_stock_changes(middle, near)
    assert [x["kind"] for x in second["window_transitions"]] == ["ENTERED_7D_WINDOW"]
    stable = changes.build_stock_changes(near, near)
    assert stable["window_transitions"] == []
    assert stable["changed"] is False


def test_removed_event_requires_current_terminal_source_evidence_for_completion_or_cancel():
    before = stock(event("evt1", "2026-09-20", "next_30d"))
    cancelled = stock(source_status="CANCELLED")
    value = changes.build_stock_changes(before, cancelled)
    assert value["removed"] == []
    assert value["status_changed"][0]["kind"] == "EVENT_CANCELLED"

    unknown = stock()
    value = changes.build_stock_changes(before, unknown)
    assert value["status_changed"] == []
    assert value["removed"][0]["kind"] == "EVENT_REMOVED_FROM_UPCOMING"


def test_unavailable_layer_fails_closed_without_false_removal():
    before = stock(event("evt1", "2026-09-20", "next_30d"))
    after = stock(status="DEFERRED")
    value = changes.build_stock_changes(before, after)
    assert value["status"] == "NO_COMPARABLE_BASELINE"
    assert value["changed"] is False
    assert value["removed"] == []


def test_apply_to_changes_updates_stock_reason_and_summary_counts():
    before = {"detail_stocks": {"002558": stock(event("evt1", "2026-09-20", "next_90d"))}}
    after = {"detail_stocks": {"002558": stock(event("evt1", "2026-09-20", "next_30d"))}}
    base_changes = {
        "stocks": {"002558": {"code": "002558", "significance": "NONE", "significance_reasons": []}},
        "groups": {},
        "market": {"significance": "NONE"},
        "events": {"significance": "NONE"},
        "summary": {},
    }
    changes.apply_to_changes(before, after, base_changes)
    stock_change = base_changes["stocks"]["002558"]
    assert stock_change["upcoming_events_change"]["changed"] is True
    assert stock_change["significance"] == "MODERATE"
    assert base_changes["summary"]["upcoming_events_entered_30d"] == 1
    assert base_changes["summary"]["upcoming_events_entered_7d"] == 0
    assert base_changes["summary"]["moderate_changes"] == 1


def main():
    tests = [
        test_new_and_date_change_are_semantic_not_countdown_noise,
        test_only_inward_30d_and_7d_threshold_crossings_are_emitted,
        test_removed_event_requires_current_terminal_source_evidence_for_completion_or_cancel,
        test_unavailable_layer_fails_closed_without_false_removal,
        test_apply_to_changes_updates_stock_reason_and_summary_counts,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"UPCOMING_EVENTS_CHANGES_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
