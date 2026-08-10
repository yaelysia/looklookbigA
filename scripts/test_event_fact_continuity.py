import json
import os
import tempfile
from datetime import datetime

import changes_since_previous
import company_event_coverage
import company_events
import event_fact_continuity


def _row(document_id="123456789", adjunct="finalpage/2026-08-09/abc.PDF"):
    return {
        "announcementId": document_id,
        "secCode": "002558",
        "announcementTitle": "2026年半年度业绩预告",
        "announcementTime": "2026-08-09 18:00:00",
        "adjunctUrl": adjunct,
    }


def _container(event):
    return {
        "status": "OK",
        "source": "CNINFO",
        "source_tier": "OFFICIAL",
        "recent": [event],
        "latest": event,
        "upcoming": [],
    }


def test_full_enriched_cache_survives_fast_overlap_without_fake_update():
    event_fact_continuity.install(company_events)
    company_event_coverage.install(company_events)
    now = datetime(2026, 8, 10, 10, 30, tzinfo=company_events.CST)
    row = _row()
    cached_event = company_events.normalize_announcement("002558", row, now)
    cached_event["facts"] = {
        "extraction_scope": "ORIGINAL_PDF_TEXT",
        "amounts": [],
        "percentages": [157.38, 183.12],
        "dates": [],
        "period": "2026H1",
        "profit_min_yuan": 2_000_000_000.0,
        "profit_max_yuan": 2_200_000_000.0,
        "yoy_min_percent": 157.38,
        "yoy_max_percent": 183.12,
        "eps_min_yuan": 1.06,
        "eps_max_yuan": 1.16,
        "document_extraction": {
            "status": "OK",
            "source_url": cached_event["source_url"],
            "parser": "pypdf",
        },
    }

    old_root = os.environ.get("MARKET_HISTORY_DIR")
    original_page = company_events._announcement_page
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["MARKET_HISTORY_DIR"] = tmp
        cache_path = company_events._event_cache_path("002558")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache = {
            "schema_version": 2,
            "code": "002558",
            "org_id": "gssz0002558",
            "source": "CNINFO",
            "source_tier": "OFFICIAL",
            "covered_start_date": "2026-07-11",
            "requested_start_date": "2026-07-11",
            "coverage_complete": True,
            "query_status": "OK",
            "updated_at_cst": "2026-08-09 18:05:00",
            "event_count": 1,
            "incomplete_ranges": [],
            "events": [cached_event],
        }
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

        calls = []

        def fake_page(code, org_id, start_date, end_date, page_num):
            calls.append((code, org_id, start_date, end_date, page_num))
            # This is the FAST overlap representation: same immutable official
            # document, but only title/API normalization is available.
            return [row], 1

        company_events._announcement_page = fake_page
        try:
            result = company_events.fetch_events_for_code("002558", 30, now=now)
        finally:
            company_events._announcement_page = original_page

        assert calls, "FAST overlap query was not exercised"
        assert result["status"] == "OK", result
        current_event = result["recent"][0]
        facts = current_event["facts"]
        assert facts["extraction_scope"] == "ORIGINAL_PDF_TEXT"
        assert facts["document_extraction"]["status"] == "OK"
        assert facts["profit_min_yuan"] == 2_000_000_000.0
        assert facts["eps_max_yuan"] == 1.16

        persisted = json.loads(cache_path.read_text(encoding="utf-8"))
        persisted_event = persisted["events"][0]
        assert persisted_event["facts"] == cached_event["facts"]

        previous = {"detail_stocks": {"002558": {"events": _container(cached_event)}}}
        current = {"detail_stocks": {"002558": {"events": _container(current_event)}}}
        event_changes = changes_since_previous._event_changes(previous, current)
        assert event_changes["new"] == []
        assert event_changes["updated"] == [], event_changes["updated"]
        assert event_changes["closed"] == []

    if old_root is None:
        os.environ.pop("MARKET_HISTORY_DIR", None)
    else:
        os.environ["MARKET_HISTORY_DIR"] = old_root


def test_different_document_does_not_inherit_old_pdf_facts():
    now = datetime(2026, 8, 10, 10, 30, tzinfo=company_events.CST)
    cached = company_events.normalize_announcement("002558", _row("old-doc"), now)
    cached["facts"] = {
        "extraction_scope": "ORIGINAL_PDF_TEXT",
        "document_extraction": {"status": "OK", "source_url": cached["source_url"], "parser": "pypdf"},
        "profit_min_yuan": 100.0,
    }
    fresh = dict(company_events.normalize_announcement("002558", _row("old-doc"), now))
    fresh["source_document_id"] = "new-doc"
    fresh["source_url"] = "https://static.cninfo.com.cn/finalpage/2026-08-10/new.PDF"
    fresh["facts"] = {"extraction_scope": "TITLE_ONLY", "amounts": [], "percentages": [], "dates": []}
    merged = event_fact_continuity.preserve_richer_facts([cached], [fresh])[0]
    assert merged["facts"]["extraction_scope"] == "TITLE_ONLY"
    assert "profit_min_yuan" not in merged["facts"]


def main():
    tests = [
        test_full_enriched_cache_survives_fast_overlap_without_fake_update,
        test_different_document_does_not_inherit_old_pdf_facts,
    ]
    for test in tests:
        test()
    print(f"EVENT_FACT_CONTINUITY_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
