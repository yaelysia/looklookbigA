import json
import tempfile
import urllib.parse
from pathlib import Path

import ownership_capital as ownership
import ownership_capital_tests_legacy as legacy


def _top_holder_payloads():
    total_rows = [
        {
            "END_DATE": "2026-06-30 00:00:00",
            "RANK": 1,
            "HOLDER_NAME": "股东甲",
            "HOLD_NUM": 300,
            "HOLD_RATIO": 30.0,
            "XZCHANGE": 10,
            "HOLDNUM_CHANGE_NAME": "增加",
            "HOLDER_NEWTYPE": "机构",
        },
        {
            "END_DATE": "2026-06-30 00:00:00",
            "RANK": 2,
            "HOLDER_NAME": "股东乙",
            "HOLD_NUM": 200,
            "HOLD_RATIO": 20.0,
            "XZCHANGE": -5,
            "HOLDNUM_CHANGE_NAME": "减少",
            "HOLDER_NEWTYPE": "自然人",
        },
        {
            "END_DATE": "2026-03-31 00:00:00",
            "RANK": 1,
            "HOLDER_NAME": "股东甲",
            "HOLD_NUM": 290,
            "HOLD_RATIO": 29.0,
            "XZCHANGE": 0,
            "HOLDNUM_CHANGE_NAME": "不变",
            "HOLDER_NEWTYPE": "机构",
        },
        {
            "END_DATE": "2026-03-31 00:00:00",
            "RANK": 2,
            "HOLDER_NAME": "股东丙",
            "HOLD_NUM": 100,
            "HOLD_RATIO": 10.0,
            "XZCHANGE": 100,
            "HOLDNUM_CHANGE_NAME": "新进",
            "HOLDER_NEWTYPE": "自然人",
        },
    ]
    float_rows = [
        {
            "END_DATE": "2026-06-30 00:00:00",
            "HOLDER_RANK": 1,
            "HOLDER_NAME": "流通股东甲",
            "HOLD_NUM": 180,
            "FREE_HOLDNUM_RATIO": 18.0,
            "XZCHANGE": 20,
            "HOLDNUM_CHANGE_NAME": "增加",
            "HOLDER_NEWTYPE": "机构",
        },
        {
            "END_DATE": "2026-06-30 00:00:00",
            "HOLDER_RANK": 2,
            "HOLDER_NAME": "流通股东乙",
            "HOLD_NUM": 120,
            "FREE_HOLDNUM_RATIO": 12.0,
            "XZCHANGE": 0,
            "HOLDNUM_CHANGE_NAME": "不变",
            "HOLDER_NEWTYPE": "自然人",
        },
        {
            "END_DATE": "2026-03-31 00:00:00",
            "HOLDER_RANK": 1,
            "HOLDER_NAME": "流通股东甲",
            "HOLD_NUM": 160,
            "FREE_HOLDNUM_RATIO": 16.0,
            "XZCHANGE": 0,
            "HOLDER_NEWTYPE": "机构",
        },
        {
            "END_DATE": "2026-03-31 00:00:00",
            "HOLDER_RANK": 2,
            "HOLDER_NAME": "流通股东丙",
            "HOLD_NUM": 90,
            "FREE_HOLDNUM_RATIO": 9.0,
            "XZCHANGE": 90,
            "HOLDNUM_CHANGE_NAME": "新进",
            "HOLDER_NEWTYPE": "自然人",
        },
    ]
    return total_rows, float_rows


def _institutional_rows(report_date):
    if report_date == "2026-06-30":
        return [
            {
                "HOLDER_CODE": "FUND001",
                "HOLDER_NAME": "基金甲",
                "HOLDER_TYPE": "基金",
                "HOLD_NUM": 100,
                "HOLD_MARKET_CAP": 1000,
                "TOTAL_SHARES_RATIO": 5.0,
                "FREE_SHARES_RATIO": 6.0,
                "HOLD_NUM_CHANGE": 10,
                "HOLD_RATIO_CHANGE": 0.5,
                "NOTICE_DATE": "2026-07-25",
            },
            {
                "HOLDER_CODE": "INS001",
                "HOLDER_NAME": "保险甲",
                "HOLDER_TYPE": "保险",
                "HOLD_NUM": 60,
                "HOLD_MARKET_CAP": 600,
                "TOTAL_SHARES_RATIO": 3.0,
                "FREE_SHARES_RATIO": 3.6,
                "HOLD_NUM_CHANGE": 10,
                "HOLD_RATIO_CHANGE": 0.5,
                "NOTICE_DATE": "2026-07-25",
            },
        ]
    if report_date == "2026-03-31":
        return [
            {
                "HOLDER_CODE": "FUND001",
                "HOLDER_NAME": "基金甲",
                "HOLDER_TYPE": "基金",
                "HOLD_NUM": 90,
                "HOLD_MARKET_CAP": 900,
                "TOTAL_SHARES_RATIO": 4.5,
                "FREE_SHARES_RATIO": 5.4,
                "HOLD_NUM_CHANGE": 5,
                "HOLD_RATIO_CHANGE": 0.2,
                "NOTICE_DATE": "2026-04-25",
            },
            {
                "HOLDER_CODE": "INS001",
                "HOLDER_NAME": "保险甲",
                "HOLDER_TYPE": "保险",
                "HOLD_NUM": 50,
                "HOLD_MARKET_CAP": 500,
                "TOTAL_SHARES_RATIO": 2.5,
                "FREE_SHARES_RATIO": 3.0,
                "HOLD_NUM_CHANGE": 0,
                "HOLD_RATIO_CHANGE": 0.0,
                "NOTICE_DATE": "2026-04-25",
            },
        ]
    raise AssertionError(report_date)


def _institutional_raw_periods():
    values = []
    for report_date in ("2026-06-30", "2026-03-31"):
        rows = _institutional_rows(report_date)
        values.append(
            {
                "report_date": report_date,
                "rows": rows,
                "source_url": f"https://example.invalid/institutional?date={report_date}",
                "page_count": 1,
                "provider_total_count": len(rows),
                "error": None,
            }
        )
    return values


def test_top_holders_preserve_period_history_and_scope_ratios():
    total_rows, float_rows = _top_holder_payloads()
    value = ownership.normalize_top_holders(
        total_rows,
        float_rows,
        "https://example.invalid/total",
        "https://example.invalid/float",
        "SZ002558",
        "2026-08-12T07:00:00+08:00",
    )
    assert value["status"] == "OK"
    assert value["as_of_date"] == "2026-06-30"
    assert value["top10_concentration_percent"] == 50.0
    assert value["float_top10_concentration_percent"] == 30.0
    total_history = value["top_shareholders"]["history"]
    float_history = value["top_float_shareholders"]["history"]
    assert [item["report_date"] for item in total_history] == ["2026-06-30", "2026-03-31"]
    assert [item["report_date"] for item in float_history] == ["2026-06-30", "2026-03-31"]
    assert "FEWER_THAN_10_REPORTED_HOLDERS" in total_history[0]["quality_flags"]
    assert "FEWER_THAN_10_REPORTED_HOLDERS" in float_history[0]["quality_flags"]
    assert total_history[0]["holders"][0]["holder_type"] == "机构"
    assert total_history[0]["holders"][0]["change_shares"] == 10.0
    assert float_history[0]["holders"][0]["hold_ratio_percent"] == 18.0
    assert value["metadata"]["holder_type_policy"] == "PROVIDER_DECLARED_ONLY; NO_NAME_BASED_CLASSIFICATION"


def test_institutional_holdings_preserve_periods_types_and_disclosure_lag():
    value = ownership.normalize_institutional_holdings(
        _institutional_raw_periods(),
        "SZ002558",
        "2026-08-12T08:00:00+08:00",
    )
    assert value["status"] == "OK"
    assert value["as_of_date"] == "2026-06-30"
    assert value["metadata"]["realtime"] is False
    assert value["metadata"]["disclosure_lag"] is True
    assert value["metadata"]["provider_type_policy"] == "PROVIDER_DECLARED_ONLY; NO_NAME_BASED_CLASSIFICATION"
    assert [item["report_date"] for item in value["history"]] == ["2026-06-30", "2026-03-31"]
    latest = value["latest"]
    assert latest["institution_count"] == 2
    assert latest["hold_shares"] == 160.0
    assert latest["hold_ratio_percent"] == 8.0
    assert latest["float_hold_ratio_percent"] == 9.6
    assert latest["fund_hold_ratio_percent"] == 5.0
    assert latest["provider_type_breakdown"][0]["provider_type"] == "保险"
    assert latest["provider_type_breakdown"][1]["provider_type"] == "基金"
    assert latest["change_from_previous"]["institution_count_delta"] == 0
    assert latest["change_from_previous"]["hold_shares_delta"] == 20.0
    assert latest["change_from_previous"]["hold_ratio_change_pp"] == 1.0
    assert latest["change_from_previous"]["fund_hold_ratio_change_pp"] == 0.5


def test_institutional_holdings_never_understate_incomplete_aggregate():
    raw = _institutional_raw_periods()[:1]
    raw[0]["rows"][1].pop("TOTAL_SHARES_RATIO")
    value = ownership.normalize_institutional_holdings(
        raw,
        "SZ002558",
        "2026-08-12T08:00:00+08:00",
    )
    assert value["status"] == "PARTIAL"
    assert value["latest"]["hold_ratio_percent"] is None
    assert "INCOMPLETE_TOTAL_SHARE_RATIO" in value["latest"]["quality_flags"]


def test_full_finalize_attaches_top_holder_and_institutional_history_without_changing_existing_slices():
    class Base:
        calls = []

        @staticmethod
        def infer_identifiers(code):
            assert code == "002558"
            return "SZ", "0.002558", "sz002558"

        @staticmethod
        def http_get(url):
            Base.calls.append(url)
            if "CapitalStockStructure" in url:
                assert "code=SZ002558" in url
                return json.dumps(legacy._payload())
            if "ShareholderResearch" in url:
                assert "code=SZ002558" in url
                return json.dumps(legacy._controller_payload())
            if "datacenter-web" in url:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                assert query["filter"] == ['(SECURITY_CODE="002558")']
                total_rows, float_rows = _top_holder_payloads()
                if query["reportName"] == ["RPT_DMSK_HOLDERS"]:
                    assert query["sortColumns"] == ["END_DATE,RANK"]
                    return json.dumps({"result": {"data": total_rows}})
                if query["reportName"] == ["RPT_F10_EH_FREEHOLDERS"]:
                    assert query["sortColumns"] == ["END_DATE,HOLDER_RANK"]
                    return json.dumps({"result": {"data": float_rows}})
            if "dataapi/zlsj/detail" in url:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)
                assert query["SHType"] == ["0"]
                assert query["SCode"] == ["002558"]
                assert query["sortField"] == ["HOLDER_CODE"]
                report_date = query["ReportDate"][0]
                rows = _institutional_rows(report_date)
                return json.dumps({"data": rows, "pages": 1, "count": len(rows)})
            raise AssertionError(url)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 16,
                    "runner_time_utc": "2026-08-11T23:00:00+00:00",
                    "detail_stocks": {"002558": {"status": "OK"}},
                }
            ),
            encoding="utf-8",
        )
        ownership.finalize_snapshot(path, Base, "FULL")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        assert len(Base.calls) == 6
        context = snapshot["detail_stocks"]["002558"]["ownership_and_capital"]
        assert context["share_structure"]["as_of_date"] == "2026-08-01"
        assert context["controllers"]["actual_controller"]["holders"][0]["name"] == "史玉柱"
        assert context["top_holders"]["as_of_date"] == "2026-06-30"
        assert context["top_holders"]["top10_concentration_percent"] == 50.0
        assert context["institutional_holdings"]["as_of_date"] == "2026-06-30"
        assert context["institutional_holdings"]["latest"]["institution_count"] == 2
        assert context["institutional_holdings"]["latest"]["hold_ratio_percent"] == 8.0
        assert context["status"] == "PARTIAL"
        assert snapshot["schema_version"] == 17
        assert snapshot["features"]["ownership_and_capital"] == "v1"
        assert snapshot["ownership_and_capital_summary"]["implemented_sections"] == [
            "share_structure",
            "controllers",
            "top_holders",
            "institutional_holdings",
        ]


def test_intraday_fast_remains_network_free_with_institutional_holdings_deferred():
    class Base:
        @staticmethod
        def infer_identifiers(code):
            raise AssertionError("FAST path must not resolve provider identifiers")

        @staticmethod
        def http_get(url):
            raise AssertionError("FAST path must not issue ownership network requests")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 16,
                    "runner_time_cst": "2026-08-12 07:00:00",
                    "detail_stocks": {"002558": {"status": "OK"}},
                }
            ),
            encoding="utf-8",
        )
        ownership.finalize_snapshot(path, Base, "INTRADAY_FAST")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        context = snapshot["detail_stocks"]["002558"]["ownership_and_capital"]
        assert context["status"] == "DEFERRED"
        assert context["share_structure"]["status"] == "DEFERRED"
        assert context["controllers"]["status"] == "DEFERRED"
        assert context["top_holders"]["status"] == "DEFERRED"
        assert context["institutional_holdings"]["status"] == "DEFERRED"
        assert context["institutional_holdings"]["metadata"]["freshness"] == "NOT_FETCHED_IN_INTRADAY_FAST"
        assert snapshot["ownership_and_capital_summary"]["status"] == "DEFERRED"


def main():
    tests = [
        legacy.test_latest_dated_row_and_float_semantics,
        legacy.test_undated_provider_row_is_not_promoted_to_current_fact,
        legacy.test_missing_required_field_is_explicitly_partial,
        legacy.test_actual_controller_is_preserved_without_guessing_controlling_shareholder,
        legacy.test_provider_declared_controlling_shareholder_is_accepted_with_date,
        test_top_holders_preserve_period_history_and_scope_ratios,
        test_institutional_holdings_preserve_periods_types_and_disclosure_lag,
        test_institutional_holdings_never_understate_incomplete_aggregate,
        test_full_finalize_attaches_top_holder_and_institutional_history_without_changing_existing_slices,
        test_intraday_fast_remains_network_free_with_institutional_holdings_deferred,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OWNERSHIP_CAPITAL_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
