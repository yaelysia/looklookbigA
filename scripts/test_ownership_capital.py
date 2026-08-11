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


def test_full_finalize_attaches_top_holder_history_without_changing_existing_slices():
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
        assert len(Base.calls) == 4
        context = snapshot["detail_stocks"]["002558"]["ownership_and_capital"]
        assert context["share_structure"]["as_of_date"] == "2026-08-01"
        assert context["controllers"]["actual_controller"]["holders"][0]["name"] == "史玉柱"
        assert context["top_holders"]["as_of_date"] == "2026-06-30"
        assert context["top_holders"]["top10_concentration_percent"] == 50.0
        assert context["status"] == "PARTIAL"
        assert snapshot["schema_version"] == 17
        assert snapshot["features"]["ownership_and_capital"] == "v1"
        assert snapshot["ownership_and_capital_summary"]["implemented_sections"] == [
            "share_structure",
            "controllers",
            "top_holders",
        ]


def test_intraday_fast_remains_network_free_with_top_holders_deferred():
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
        assert context["top_holders"]["metadata"]["freshness"] == "NOT_FETCHED_IN_INTRADAY_FAST"
        assert snapshot["ownership_and_capital_summary"]["status"] == "DEFERRED"


def main():
    tests = [
        legacy.test_latest_dated_row_and_float_semantics,
        legacy.test_undated_provider_row_is_not_promoted_to_current_fact,
        legacy.test_missing_required_field_is_explicitly_partial,
        legacy.test_actual_controller_is_preserved_without_guessing_controlling_shareholder,
        legacy.test_provider_declared_controlling_shareholder_is_accepted_with_date,
        test_top_holders_preserve_period_history_and_scope_ratios,
        test_full_finalize_attaches_top_holder_history_without_changing_existing_slices,
        test_intraday_fast_remains_network_free_with_top_holders_deferred,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OWNERSHIP_CAPITAL_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
