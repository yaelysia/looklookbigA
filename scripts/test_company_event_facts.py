import company_event_facts as facts


def test_earnings_forecast_pdf_table_extraction():
    text = """
    一、本期业绩预计情况
    1. 业绩预告期间：2026 年 1 月 1 日至 2026 年 6 月 30 日。
    单位：万元
    项目 本报告期 上年同期
    归属于上市公司股东的净利润 200,000 ～ 220,000 77,704.81
    比上年同期增长 157.38% ～ 183.12%
    扣除非经常性损益后的净利润 220,000 ～ 240,000 80,662.19
    比上年同期增长 172.74% ～ 197.54%
    基本每股收益（元/股） 1.06 ～ 1.16 0.42
    """
    value = {"extraction_scope": "ORIGINAL_PDF_TEXT"}
    facts._earnings_facts(" ".join(text.split()), value)
    assert value["profit_min_yuan"] == 2_000_000_000.0
    assert value["profit_max_yuan"] == 2_200_000_000.0
    assert value["yoy_min_percent"] == 157.38
    assert value["yoy_max_percent"] == 183.12
    assert value["eps_min_yuan"] == 1.06
    assert value["eps_max_yuan"] == 1.16
    assert value["period_start_date"] == "2026-01-01"
    assert value["period_end_date"] == "2026-06-30"
    print("PASS earnings_pdf_table")


def test_buyback_pdf_fact_extraction():
    text = "公司本次回购资金总额不低于1亿元且不超过2亿元，回购价格不超过30元/股。"
    value = {}
    facts._buyback_facts(text, value)
    assert value["amount_min_yuan"] == 100_000_000.0
    assert value["amount_max_yuan"] == 200_000_000.0
    assert value["price_cap_yuan_per_share"] == 30.0
    print("PASS buyback_pdf_facts")


def test_document_failure_preserves_title_facts():
    original_download = facts._download_pdf
    event = {
        "event_id": "cninfo:test",
        "event_type": "EARNINGS_FORECAST",
        "title": "2026年半年度业绩预告",
        "source_url": "https://static.cninfo.com.cn/test.pdf",
        "facts": {
            "extraction_scope": "TITLE_ONLY",
            "period": "2026H1",
        },
    }
    try:
        facts._download_pdf = lambda url: (_ for _ in ()).throw(OSError("forced"))
        event, ok = facts.enrich_event(event)
    finally:
        facts._download_pdf = original_download
    assert ok is False
    assert event["facts"]["period"] == "2026H1"
    assert event["facts"]["extraction_scope"] == "TITLE_ONLY"
    assert event["facts"]["document_extraction"]["status"] == "UNAVAILABLE"
    assert "forced" in event["facts"]["document_extraction"]["error"]
    print("PASS document_failure_preserves_facts")


def test_same_host_https_redirect_is_allowed():
    handler = facts._CninfoPdfRedirectHandler()
    request = facts.urllib.request.Request("https://static.cninfo.com.cn/a.pdf")
    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://static.cninfo.com.cn/finalpage/a.pdf?download=1",
    )
    assert redirected.full_url == "https://static.cninfo.com.cn/finalpage/a.pdf?download=1"
    print("PASS same_host_https_redirect_allowed")


def test_cross_host_redirect_is_rejected():
    handler = facts._CninfoPdfRedirectHandler()
    request = facts.urllib.request.Request("https://static.cninfo.com.cn/a.pdf")
    try:
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://example.com/a.pdf",
        )
    except ValueError as exc:
        assert "host is not allowed" in str(exc)
    else:
        raise AssertionError("cross-host redirect must be rejected")
    print("PASS cross_host_redirect_rejected")


def test_https_to_http_redirect_is_rejected():
    handler = facts._CninfoPdfRedirectHandler()
    request = facts.urllib.request.Request("https://static.cninfo.com.cn/a.pdf")
    try:
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "http://static.cninfo.com.cn/a.pdf",
        )
    except ValueError as exc:
        assert "must use HTTPS" in str(exc)
    else:
        raise AssertionError("HTTPS-to-HTTP redirect must be rejected")
    print("PASS http_downgrade_redirect_rejected")


def test_final_response_url_is_revalidated_before_read():
    original_build_opener = facts.urllib.request.build_opener
    state = {"read_called": False}

    class FakeResponse:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def geturl(self):
            return "https://cdn.example.com/announcement.pdf"

        def read(self, size=-1):
            state["read_called"] = True
            return b"%PDF-1.7 fixture"

    class FakeOpener:
        def open(self, req, timeout=None):
            return FakeResponse()

    try:
        facts.urllib.request.build_opener = lambda *handlers: FakeOpener()
        try:
            facts._download_pdf("https://static.cninfo.com.cn/announcement.pdf")
        except ValueError as exc:
            assert "final CNINFO PDF host is not allowed" in str(exc)
        else:
            raise AssertionError("untrusted final response URL must be rejected")
    finally:
        facts.urllib.request.build_opener = original_build_opener

    assert state["read_called"] is False
    print("PASS final_response_url_revalidated_before_read")


def test_invalid_initial_url_variants_are_rejected():
    invalid = [
        "http://static.cninfo.com.cn/a.pdf",
        "https://static.cninfo.com.cn.evil.example/a.pdf",
        "https://user@static.cninfo.com.cn/a.pdf",
        "https://static.cninfo.com.cn:8443/a.pdf",
    ]
    for url in invalid:
        try:
            facts._validate_cninfo_pdf_url(url)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid official PDF URL accepted: {url}")
    print("PASS invalid_initial_pdf_urls_rejected")


def main():
    tests = [
        test_earnings_forecast_pdf_table_extraction,
        test_buyback_pdf_fact_extraction,
        test_document_failure_preserves_title_facts,
        test_same_host_https_redirect_is_allowed,
        test_cross_host_redirect_is_rejected,
        test_https_to_http_redirect_is_rejected,
        test_final_response_url_is_revalidated_before_read,
        test_invalid_initial_url_variants_are_rejected,
    ]
    for test in tests:
        test()
    print(f"COMPANY_EVENT_FACT_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
