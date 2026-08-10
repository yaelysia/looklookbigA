import json
import urllib.parse

import capital_flow_context as capital
import capital_flow_margin_bridge


capital_flow_margin_bridge.install(capital)


def test_margin_query_uses_provider_native_date_sort_and_scode_filter():
    seen = []

    class Base:
        @staticmethod
        def http_get(url):
            seen.append(url)
            return json.dumps({
                "success": True,
                "result": {
                    "data": [
                        {
                            "DATE": "2026-08-07 00:00:00",
                            "SCODE": "002558",
                            "RZYE": 1000,
                            "RZMRE": 120,
                            "RZCHE": 90,
                            "RQYE": 5,
                            "RZRQYE": 1005,
                        },
                        {
                            "DATE": "2026-08-06 00:00:00",
                            "SCODE": "002558",
                            "RZYE": 950,
                            "RZMRE": 100,
                            "RZCHE": 80,
                            "RQYE": 4,
                            "RZRQYE": 954,
                        },
                    ]
                },
            })

    records, _ = capital.fetch_margin_history(Base, "002558")
    assert len(seen) == 1
    parsed = urllib.parse.urlparse(seen[0])
    query = urllib.parse.parse_qs(parsed.query)
    assert query["reportName"] == ["RPTA_WEB_RZRQ_GGMX"]
    assert query["sortColumns"] == ["DATE"]
    assert query["filter"] == ["(SCODE='002558')"]
    assert records[0]["trade_date"] == "2026-08-07"
    assert records[0]["financing_balance"] == 1000.0
    assert records[0]["financing_net_buy_amount"] == 30.0
    assert records[0]["margin_balance"] == 1005.0


def test_margin_provider_rejection_is_explicit():
    class Base:
        @staticmethod
        def http_get(url):
            return json.dumps({"success": False, "code": 9501, "message": "bad query", "result": None})

    try:
        capital.fetch_margin_history(Base, "002558")
    except RuntimeError as exc:
        assert "rejected query" in str(exc)
        assert "9501" in str(exc)
    else:
        raise AssertionError("provider rejection must not look like empty valid history")


def main():
    tests = [
        test_margin_query_uses_provider_native_date_sort_and_scode_filter,
        test_margin_provider_rejection_is_explicit,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"CAPITAL_FLOW_MARGIN_PROVIDER_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
