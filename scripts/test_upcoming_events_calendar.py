import upcoming_events_calendar as calendar


def _source(layer, event_id, document_id):
    return {
        "provider": "CNINFO",
        "source_tier": "OFFICIAL",
        "source_layer": layer,
        "source_event_id": event_id,
        "source_document_id": document_id,
    }


def _event(event_id, event_type, event_date, title, layer, document_id, importance="MEDIUM"):
    return {
        "event_id": f"upcoming:{event_id}:{event_type}:{event_date}",
        "event_type": event_type,
        "title": title,
        "event_date": event_date,
        "date_end": None,
        "date_certainty": "CONFIRMED_DATE",
        "days_until_event": None,
        "importance": importance,
        "status": "UPCOMING",
        "source_event_id": event_id,
        "source_relations": [_source(layer, event_id, document_id)],
        "details": {},
    }


def test_title_scoped_dates_require_explicit_title_semantics():
    dividend = {
        "event_type": "DIVIDEND",
        "title": "2025年度权益分派：股权登记日2026年8月15日，除权除息日2026年8月17日",
    }
    assert calendar._title_scoped_dates(dividend) == [
        ("DIVIDEND_RECORD_DATE", "2026-08-15", "record_date"),
        ("DIVIDEND_EX_DATE", "2026-08-17", "ex_dividend_date"),
    ]
    assert calendar._title_scoped_dates(
        {"event_type": "DIVIDEND", "title": "2025年度权益分派实施公告"}
    ) == []
    assert calendar._title_scoped_dates(
        {
            "event_type": "OTHER",
            "title": "定于2026年8月18日召开2026年第一次临时股东大会",
        }
    ) == [("SHAREHOLDER_MEETING", "2026-08-18", "meeting_date")]
    assert calendar._title_scoped_dates(
        {
            "event_type": "SUSPENSION_RESUMPTION",
            "title": "公司将于2026年8月17日复牌",
        }
    ) == [("RESUMPTION", "2026-08-17", "resumption_date")]


def test_semantic_dedupe_merges_cross_layer_document_identity():
    first = _event(
        "cninfo:first",
        "UNLOCK",
        "2026-08-20",
        "限售股份上市流通公告",
        "company_events",
        "doc-1",
        "HIGH",
    )
    second = _event(
        "ownership:second",
        "UNLOCK",
        "2026-08-20",
        "限售股份上市流通公告",
        "ownership_and_capital.unlocks",
        "doc-1",
        "HIGH",
    )
    second["details"]["unlock_shares"] = 100.0
    merged = calendar._semantic_dedupe([first, second])
    assert len(merged) == 1
    assert len(merged[0]["source_relations"]) == 2
    assert merged[0]["details"]["unlock_shares"] == 100.0


def test_trading_day_context_is_verified_and_fail_closed():
    weekend = calendar._trading_day_context("2026-08-15")
    assert weekend["verification_status"] == "VERIFIED"
    assert weekend["is_trading_day"] is False
    assert weekend["previous_trading_day"] == "2026-08-14"
    assert weekend["next_trading_day"] == "2026-08-17"
    assert weekend["near_trading_day"] is True

    outside = calendar._trading_day_context("2027-01-04")
    assert outside["verification_status"] == "UNVERIFIED"
    assert outside["is_trading_day"] is None
    assert outside["near_trading_day"] is None


def test_augment_adds_confirmed_titles_overlap_and_calendar_context():
    stock = {
        "events": {
            "status": "OK",
            "upcoming": [
                {
                    "event_id": "cninfo:dividend",
                    "event_type": "DIVIDEND",
                    "title": "权益分派 股权登记日2026年8月15日",
                    "effective_date": "2026-08-15",
                    "importance": "HIGH",
                    "status": "OPEN",
                    "source": "CNINFO",
                    "source_tier": "OFFICIAL",
                    "source_document_id": "dividend-doc",
                },
                {
                    "event_id": "cninfo:mna",
                    "event_type": "M&A",
                    "title": "重大资产重组进展公告",
                    "effective_date": "2026-08-21",
                    "importance": "HIGH",
                    "status": "OPEN",
                    "source": "CNINFO",
                    "source_tier": "OFFICIAL",
                    "source_document_id": "mna-doc",
                },
            ],
        },
        "upcoming_events": {
            "status": "OK",
            "as_of_date": "2026-08-13",
            "nearest": None,
            "next_7d": [
                _event(
                    "cninfo:buyback",
                    "BUYBACK_WINDOW_START",
                    "2026-08-15",
                    "回购方案: execution window starts",
                    "ownership_and_capital.buyback_and_holder_plans.buybacks",
                    "buyback-doc",
                )
            ],
            "next_30d": [],
            "next_90d": [],
            "later": [],
            "calendar_summary": {},
            "metadata": {},
            "provenance": {"source_layers": []},
        },
    }
    value = calendar.augment_stock(stock, "2026-08-13")
    events = value["next_7d"]
    assert {event["event_type"] for event in events} == {
        "BUYBACK_WINDOW_START",
        "DIVIDEND_RECORD_DATE",
    }
    assert all(event["event_type"] != "M&A" for event in events)
    buyback = next(event for event in events if event["event_type"] == "BUYBACK_WINDOW_START")
    assert buyback["overlap_context"]["same_day_event_count"] == 2
    assert buyback["overlap_context"]["overlaps_high_importance_event"] is True
    assert value["calendar_summary"]["same_day_overlap_date_count"] == 1
    assert value["calendar_summary"]["title_scoped_confirmed_event_count"] == 1


def main():
    tests = [
        test_title_scoped_dates_require_explicit_title_semantics,
        test_semantic_dedupe_merges_cross_layer_document_identity,
        test_trading_day_context_is_verified_and_fail_closed,
        test_augment_adds_confirmed_titles_overlap_and_calendar_context,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"UPCOMING_EVENTS_CALENDAR_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
