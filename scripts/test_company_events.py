import os
import tempfile
from datetime import datetime

import company_events as events


CST = events.CST


def _row(
    announcement_id,
    title,
    when_ms=1786074000000,
    code="002558",
    adjunct="finalpage/2026-08-07/1212345678.PDF",
    content=None,
):
    value = {
        "announcementId": announcement_id,
        "announcementTitle": title,
        "announcementTime": when_ms,
        "secCode": code,
        "adjunctUrl": adjunct,
    }
    if content:
        value["announcementContent"] = content
    return value


def test_event_type_classification():
    cases = {
        "2026年半年度业绩预告": "EARNINGS_FORECAST",
        "2026年半年度业绩快报": "EARNINGS_EXPRESS",
        "2026年半年度报告": "PERIODIC_REPORT",
        "关于回购公司股份的公告": "BUYBACK",
        "关于控股股东增持股份的公告": "HOLDER_INCREASE",
        "关于股东减持股份的预披露公告": "HOLDER_DECREASE",
        "关于限售股份上市流通的提示性公告": "UNLOCK",
        "关于收到重大项目中标通知书的公告": "MAJOR_CONTRACT",
        "重大资产重组进展公告": "M&A",
        "2025年度权益分派实施公告": "DIVIDEND",
        "2026年限制性股票激励计划": "EQUITY_INCENTIVE",
        "关于重大诉讼进展的公告": "LITIGATION",
        "关于收到监管警示函的公告": "REGULATORY",
        "股票交易异常波动公告": "TRADING_ANOMALY",
        "关于公司股票复牌的公告": "SUSPENSION_RESUMPTION",
        "投资者关系活动记录表": "INVESTOR_RELATIONS",
        "关于董事会换届的公告": "OTHER",
    }
    for title, expected in cases.items():
        assert events.classify_event(title) == expected, (title, expected)
    print("PASS event_type_classification")


def test_normalization_stable_id_and_fact_extraction():
    now = datetime(2026, 8, 7, 18, 0, tzinfo=CST)
    row = _row(
        "1212345678",
        "关于回购公司股份的公告",
        content="回购金额不低于1亿元且不超过2亿元，回购价格不超过30元/股，实施日期2026年8月15日。",
    )
    event = events.normalize_announcement("002558", row, now)
    assert event["event_id"] == "cninfo:1212345678"
    assert event["event_type"] == "BUYBACK"
    assert event["source"] == "CNINFO"
    assert event["source_tier"] == "OFFICIAL"
    assert event["source_url"].startswith("https://static.cninfo.com.cn/")
    assert event["facts"]["amount_min_yuan"] == 100000000.0
    assert event["facts"]["amount_max_yuan"] == 200000000.0
    assert event["facts"]["price_cap_yuan_per_share"] == 30.0
    assert event["effective_date"] == "2026-08-15"
    assert event["fetched_at"] == "2026-08-07T18:00:00+08:00"
    print("PASS stable_id_and_facts")


def test_earnings_fact_range_from_api_snippet():
    now = datetime(2026, 8, 7, 18, 0, tzinfo=CST)
    row = _row(
        "earn-1",
        "2026年半年度业绩预告",
        content="预计归母净利润1亿元至2亿元，同比增长20%至40%。",
    )
    event = events.normalize_announcement("002558", row, now)
    facts = event["facts"]
    assert facts["period"] == "2026H1"
    assert facts["profit_min_yuan"] == 100000000.0
    assert facts["profit_max_yuan"] == 200000000.0
    assert facts["yoy_min_percent"] == 20.0
    assert facts["yoy_max_percent"] == 40.0
    assert facts["extraction_scope"] == "TITLE_AND_API_SNIPPET"
    print("PASS earnings_fact_range")


def test_correction_and_progress_relations_do_not_overwrite_original():
    base = {
        "event_id": "cninfo:1",
        "event_type": "PERIODIC_REPORT",
        "title": "2025年年度报告",
        "published_at": "2026-04-01T18:00:00+08:00",
        "related_event_id": None,
        "supersedes_event_id": None,
    }
    correction = {
        "event_id": "cninfo:2",
        "event_type": "PERIODIC_REPORT",
        "title": "2025年年度报告（更正版）",
        "published_at": "2026-04-03T18:00:00+08:00",
        "related_event_id": None,
        "supersedes_event_id": None,
    }
    linked = events.link_related_events([base.copy(), correction.copy()])
    by_id = {x["event_id"]: x for x in linked}
    assert by_id["cninfo:1"]["supersedes_event_id"] is None
    assert by_id["cninfo:2"]["related_event_id"] == "cninfo:1"
    assert by_id["cninfo:2"]["supersedes_event_id"] == "cninfo:1"
    assert len(by_id) == 2
    print("PASS correction_relation")


def test_incremental_cache_and_provider_failure_semantics():
    old_root = os.environ.get("MARKET_HISTORY_DIR")
    original_load_map = events._load_stock_map
    original_query = events._query_announcements
    now = datetime(2026, 8, 7, 18, 0, tzinfo=CST)
    captured = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_HISTORY_DIR"] = tmp

            def fake_map(_now):
                return {"002558": {"org_id": "gssz0002558", "name": "测试"}}, {"state": "REFRESHED"}

            def first_query(code, org_id, start, end):
                captured.append((start, end))
                return [
                    _row("evt-a", "关于回购公司股份的公告", when_ms=int(datetime(2026, 8, 5, 9, 0, tzinfo=CST).timestamp() * 1000)),
                    _row("evt-b", "关于召开2026年第一次临时股东会的通知，会议日期2026年8月20日", when_ms=int(datetime(2026, 8, 6, 9, 0, tzinfo=CST).timestamp() * 1000)),
                ], {
                    "total_record_num": 2,
                    "pages_total": 1,
                    "pages_requested": 1,
                    "rows_received": 2,
                    "complete": True,
                    "errors": [],
                }

            events._load_stock_map = fake_map
            events._query_announcements = first_query
            result = events.fetch_events_for_code("002558", 30, now=now)
            assert result["status"] == "OK"
            assert result["cache"]["state"] == "BOOTSTRAP"
            assert len(result["recent"]) == 2
            assert result["upcoming"][0]["effective_date"] == "2026-08-20"
            assert captured[0][0] == "2026-07-08"

            cache = events._read_json(events._event_cache_path("002558"))
            assert cache["org_id"] == "gssz0002558"
            assert len(cache["events"]) == 2

            def second_query(code, org_id, start, end):
                captured.append((start, end))
                return [_row("evt-c", "股票交易异常波动公告", when_ms=int(datetime(2026, 8, 7, 17, 0, tzinfo=CST).timestamp() * 1000))], {
                    "total_record_num": 1,
                    "pages_total": 1,
                    "pages_requested": 1,
                    "rows_received": 1,
                    "complete": True,
                    "errors": [],
                }

            events._query_announcements = second_query
            updated = events.fetch_events_for_code("002558", 30, now=now)
            assert updated["status"] == "OK"
            assert updated["cache"]["state"] == "REFRESHED"
            assert updated["cache"]["refresh_mode"] == "INCREMENTAL_OVERLAP"
            assert len(updated["recent"]) == 3
            assert captured[1][0] >= "2026-07-29"

            def failing_query(*args, **kwargs):
                raise OSError("forced provider failure")

            events._query_announcements = failing_query
            degraded = events.fetch_events_for_code("002558", 30, now=now)
            assert degraded["status"] == "DEGRADED"
            assert degraded["cache"]["state"] == "STALE_FALLBACK"
            assert len(degraded["recent"]) == 3
            assert "forced provider failure" in degraded["error"]

            no_cache = events.fetch_events_for_code("600000", 30, now=now)
            assert no_cache["status"] == "ERROR"
            assert no_cache["recent"] == []
            assert no_cache["no_events_reason"] == "PROVIDER_FAILED_NO_CACHE"
            assert no_cache["provider_health"]["status"] == "ERROR"
    finally:
        events._load_stock_map = original_load_map
        events._query_announcements = original_query
        if old_root is None:
            os.environ.pop("MARKET_HISTORY_DIR", None)
        else:
            os.environ["MARKET_HISTORY_DIR"] = old_root
    print("PASS incremental_cache_and_failure_semantics")


def test_partial_pagination_is_not_silent_success():
    old_root = os.environ.get("MARKET_HISTORY_DIR")
    original_map = events._load_stock_map
    original_query = events._query_announcements
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_HISTORY_DIR"] = tmp
            events._load_stock_map = lambda now: ({"002558": {"org_id": "org"}}, {"state": "REFRESHED"})
            events._query_announcements = lambda *args: (
                [_row("partial-1", "关于回购公司股份的公告")],
                {
                    "total_record_num": 200,
                    "pages_total": 7,
                    "pages_requested": 6,
                    "rows_received": 180,
                    "complete": False,
                    "errors": ["query capped at 6/7 pages"],
                },
            )
            result = events.fetch_events_for_code("002558", 30, now=datetime(2026, 8, 7, 18, 0, tzinfo=CST))
            assert result["status"] == "PARTIAL"
            assert result["recent"]
            assert result["provider_health"]["status"] == "PARTIAL"
            assert result["error"]
    finally:
        events._load_stock_map = original_map
        events._query_announcements = original_query
        if old_root is None:
            os.environ.pop("MARKET_HISTORY_DIR", None)
        else:
            os.environ["MARKET_HISTORY_DIR"] = old_root
    print("PASS partial_pagination")


def main():
    tests = [
        test_event_type_classification,
        test_normalization_stable_id_and_fact_extraction,
        test_earnings_fact_range_from_api_snippet,
        test_correction_and_progress_relations_do_not_overwrite_original,
        test_incremental_cache_and_provider_failure_semantics,
        test_partial_pagination_is_not_silent_success,
    ]
    for test in tests:
        test()
    print(f"COMPANY_EVENT_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
