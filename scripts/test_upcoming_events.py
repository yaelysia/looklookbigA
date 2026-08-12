import json
import tempfile
from pathlib import Path

import upcoming_events


def _stock():
    return {
        "events": {
            "status": "OK",
            "upcoming": [
                {
                    "event_id": "cninfo:unlock1",
                    "event_type": "UNLOCK",
                    "title": "限售股份上市流通公告",
                    "effective_date": "2026-08-20",
                    "importance": "HIGH",
                    "status": "OPEN",
                    "source": "CNINFO",
                    "source_tier": "OFFICIAL",
                    "source_document_id": "unlock-doc",
                    "source_url": "https://static.cninfo.com.cn/unlock.pdf",
                    "facts": {
                        "unlock_date": "2026-08-20",
                        "extraction_scope": "TITLE_AND_API_SNIPPET",
                    },
                },
                {
                    "event_id": "cninfo:mna1",
                    "event_type": "M&A",
                    "title": "重大资产重组进展公告",
                    "effective_date": "2026-08-25",
                    "importance": "HIGH",
                    "status": "OPEN",
                    "source": "CNINFO",
                    "source_tier": "OFFICIAL",
                    "facts": {"dates": ["2026-08-25"]},
                },
            ],
        },
        "ownership_and_capital": {
            "unlocks": {
                "status": "OK",
                "upcoming": [
                    {
                        "event_id": "cninfo:unlock1",
                        "unlock_date": "2026-08-20",
                        "status": "OPEN",
                        "title": "限售股份上市流通公告",
                        "unlock_shares": 100.0,
                        "unlock_ratio_total_percent": 2.0,
                        "unlock_ratio_float_percent": 2.5,
                        "importance": "HIGH",
                        "provenance": {
                            "provider": "CNINFO",
                            "source_tier": "OFFICIAL",
                            "source_document_id": "unlock-doc",
                            "source_url": "https://static.cninfo.com.cn/unlock.pdf",
                        },
                    }
                ],
            },
            "buyback_and_holder_plans": {
                "status": "PARTIAL",
                "buybacks": {
                    "history": [
                        {
                            "event_id": "cninfo:buyback1",
                            "event_type": "BUYBACK",
                            "status": "OPEN",
                            "title": "股份回购方案",
                            "active_execution_window": "UNKNOWN",
                            "window_start_date": "2026-08-15",
                            "window_end_date": "2026-09-01",
                            "provenance": {
                                "provider": "CNINFO",
                                "source_tier": "OFFICIAL",
                                "source_document_id": "buyback-doc",
                            },
                        }
                    ]
                },
                "holder_increase_plans": {"history": []},
                "holder_decrease_plans": {
                    "history": [
                        {
                            "event_id": "cninfo:decrease1",
                            "event_type": "HOLDER_DECREASE",
                            "status": "OPEN",
                            "title": "股东减持计划",
                            "active_execution_window": "UNKNOWN",
                            "window_start_date": None,
                            "window_end_date": "2026-12-15",
                            "provenance": {
                                "provider": "CNINFO",
                                "source_tier": "OFFICIAL",
                            },
                        }
                    ]
                },
            },
        },
    }


def test_normalizes_unlock_and_explicit_plan_windows():
    value = upcoming_events.build_upcoming_events(_stock(), "2026-08-13")
    assert value["status"] == "PARTIAL"
    assert value["calendar_summary"]["event_count"] == 4
    assert len(value["next_7d"]) == 2
    assert len(value["next_30d"]) == 1
    assert len(value["next_90d"]) == 0
    assert len(value["later"]) == 1

    unlock = [item for item in value["next_7d"] if item["event_type"] == "UNLOCK"][0]
    assert unlock["event_date"] == "2026-08-20"
    assert unlock["date_certainty"] == "CONFIRMED_DATE"
    assert unlock["days_until_event"] == 7
    assert len(unlock["source_relations"]) == 2
    assert unlock["details"]["unlock_shares"] == 100.0

    start = [item for item in value["next_7d"] if item["event_type"] == "BUYBACK_WINDOW_START"][0]
    assert start["event_date"] == "2026-08-15"
    end = value["next_30d"][0]
    assert end["event_type"] == "BUYBACK_WINDOW_END"
    assert end["event_date"] == "2026-09-01"


def test_generic_effective_date_is_fail_closed():
    value = upcoming_events.build_upcoming_events(_stock(), "2026-08-13")
    assert value["metadata"]["excluded_unproven_company_event_count"] == 1
    assert all(item["event_type"] != "M&A" for bucket in ("next_7d", "next_30d", "next_90d", "later") for item in value[bucket])


def test_terminal_or_past_plan_dates_are_not_emitted():
    stock = _stock()
    stock["ownership_and_capital"]["buyback_and_holder_plans"]["buybacks"]["history"][0]["status"] = "COMPLETED"
    stock["ownership_and_capital"]["buyback_and_holder_plans"]["holder_decrease_plans"]["history"][0]["window_end_date"] = "2026-08-01"
    value = upcoming_events.build_upcoming_events(stock, "2026-08-13")
    types = [
        item["event_type"]
        for bucket in ("next_7d", "next_30d", "next_90d", "later")
        for item in value[bucket]
    ]
    assert types == ["UNLOCK"]


def test_finalize_attaches_snapshot_feature_and_schema():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 17,
                    "runner_time_cst": "2026-08-13 03:00:00",
                    "detail_stocks": {"002558": _stock()},
                }
            ),
            encoding="utf-8",
        )
        upcoming_events.finalize_snapshot(path)
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        assert snapshot["schema_version"] == 18
        assert snapshot["features"]["upcoming_events"] == "v1"
        assert snapshot["detail_stocks"]["002558"]["upcoming_events"]["nearest"]["event_type"] == "BUYBACK_WINDOW_START"
        assert snapshot["upcoming_events_summary"]["implemented_sources"] == [
            "official_company_unlock_dates",
            "ownership_unlock_state",
            "ownership_plan_explicit_windows",
        ]


def main():
    tests = [
        test_normalizes_unlock_and_explicit_plan_windows,
        test_generic_effective_date_is_fail_closed,
        test_terminal_or_past_plan_dates_are_not_emitted,
        test_finalize_attaches_snapshot_feature_and_schema,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"UPCOMING_EVENTS_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
