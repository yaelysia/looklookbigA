import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import company_event_coverage
import company_events


company_event_coverage.install(company_events)


def _row(event_id, published="2026-08-01 09:00:00"):
    return {
        "announcementId": str(event_id),
        "announcementTitle": f"测试公告 {event_id}",
        "announcementTime": published,
        "secCode": "002558",
        "adjunctUrl": f"finalpage/2026-08-01/{event_id}.PDF",
    }


def test_legacy_partial_cache_forces_backfill():
    desired = datetime(2026, 7, 9).date()
    cache = {
        "query_status": "PARTIAL",
        "covered_start_date": desired.isoformat(),
        "events": [_row("legacy")],
    }
    start, mode = company_events._resolve_query_start(cache, desired, datetime(2026, 8, 8, tzinfo=company_events.CST))
    assert start == desired
    assert mode == "BACKFILL_INCOMPLETE"
    print("PASS legacy_partial_forces_backfill")


def test_partial_run_cannot_become_recent_only_incremental():
    old_root = os.environ.get("MARKET_HISTORY_DIR")
    old_load_map = company_events._load_stock_map
    old_page = company_events._announcement_page
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_HISTORY_DIR"] = tmp
            company_events._load_stock_map = lambda now: (
                {"002558": {"org_id": "gssz0002558", "name": "fixture"}},
                {"state": "FIXTURE", "source": "fixture", "error": None},
            )

            calls = []
            fail_middle_page = {"enabled": True}

            def page(code, org_id, start_date, end_date, page_num):
                calls.append((start_date, end_date, page_num))
                total = company_events.PAGE_SIZE * 3
                if page_num == 2 and fail_middle_page["enabled"]:
                    raise RuntimeError("fixture page 2 failure")
                return [_row(f"{page_num}-{len(calls)}")], total

            company_events._announcement_page = page
            now = datetime(2026, 8, 8, 10, 0, tzinfo=company_events.CST)
            desired_start = (now.date() - timedelta(days=30)).isoformat()

            first = company_events.fetch_events_for_code("002558", 30, now=now)
            assert first["status"] == "PARTIAL"
            assert first["cache"]["coverage_complete"] is False
            assert first["cache"]["covered_start_date"] is None
            assert first["cache"]["incomplete_ranges"]
            assert any(item.get("reason") == "PAGE_REQUEST_FAILED" for item in first["cache"]["incomplete_ranges"])

            cache_path = Path(tmp) / "events" / "002558.json"
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            assert cached["query_status"] == "PARTIAL"
            assert cached["coverage_complete"] is False
            assert cached["covered_start_date"] is None

            fail_middle_page["enabled"] = False
            calls.clear()
            second = company_events.fetch_events_for_code("002558", 30, now=now + timedelta(minutes=1))
            assert second["status"] == "OK"
            assert second["cache"]["refresh_mode"] == "BACKFILL_INCOMPLETE"
            assert calls[0][0] == desired_start
            assert second["cache"]["coverage_complete"] is True
            assert second["cache"]["covered_start_date"] == desired_start

            cached2 = json.loads(cache_path.read_text(encoding="utf-8"))
            assert cached2["query_status"] == "OK"
            assert cached2["coverage_complete"] is True
            assert cached2["covered_start_date"] == desired_start
    finally:
        company_events._load_stock_map = old_load_map
        company_events._announcement_page = old_page
        if old_root is None:
            os.environ.pop("MARKET_HISTORY_DIR", None)
        else:
            os.environ["MARKET_HISTORY_DIR"] = old_root
    print("PASS partial_run_backfills_before_incremental")


def test_page_cap_is_split_by_date_instead_of_truncated():
    old_page = company_events._announcement_page
    try:
        calls = []
        root_start = "2026-07-10"
        root_end = "2026-08-08"

        def page(code, org_id, start_date, end_date, page_num):
            calls.append((start_date, end_date, page_num))
            if start_date == root_start and end_date == root_end:
                return [_row("root")], company_events.PAGE_SIZE * (company_events.MAX_PAGES + 2)
            return [_row(f"{start_date}-{end_date}")], 1

        company_events._announcement_page = page
        rows, meta = company_events._query_announcements("002558", "gssz0002558", root_start, root_end)
        assert meta["complete"] is True
        assert meta["split_used"] is True
        assert meta["missing_ranges"] == []
        assert len(rows) >= 2
        assert any(call[0] != root_start or call[1] != root_end for call in calls)
    finally:
        company_events._announcement_page = old_page
    print("PASS page_cap_split_by_date")


def test_unsplittable_page_cap_stays_partial():
    old_page = company_events._announcement_page
    try:
        def page(code, org_id, start_date, end_date, page_num):
            return [_row(f"p{page_num}")], company_events.PAGE_SIZE * (company_events.MAX_PAGES + 2)

        company_events._announcement_page = page
        rows, meta = company_events._query_announcements("002558", "gssz0002558", "2026-08-08", "2026-08-08")
        assert rows
        assert meta["complete"] is False
        assert any(item.get("reason") == "PAGE_CAP_EXCEEDED" for item in meta["missing_ranges"])
    finally:
        company_events._announcement_page = old_page
    print("PASS unsplittable_cap_remains_partial")


def main():
    tests = [
        test_legacy_partial_cache_forces_backfill,
        test_partial_run_cannot_become_recent_only_incremental,
        test_page_cap_is_split_by_date_instead_of_truncated,
        test_unsplittable_page_cap_stays_partial,
    ]
    for test in tests:
        test()
    print(f"COMPANY_EVENT_COVERAGE_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
