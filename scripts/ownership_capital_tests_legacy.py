import json
import tempfile
from pathlib import Path

import ownership_capital as ownership


def _payload():
    return {
        "lngbbd": [
            {
                "END_DATE": "2026-06-30 00:00:00",
                "TOTAL_SHARES": 1000,
                "UNLIMITED_SHARES": 800,
                "LISTED_A_SHARES": 760,
                "LIMITED_SHARES": 200,
                "FREE_SHARES": 610,
                "CHANGE_REASON": "定期报告",
            },
            {
                "END_DATE": "2026-08-01 00:00:00",
                "TOTAL_SHARES": 1200,
                "UNLIMITED_SHARES": 900,
                "LISTED_A_SHARES": 850,
                "LIMITED_SHARES": 300,
                "FREE_SHARES": 700,
                "CHANGE_REASON": "股份变动",
            },
        ],
        "gbjg": [
            {
                "TOTAL_SHARES": 9999,
                "LISTED_A_SHARES": 9999,
                "LIMITED_SHARES": 0,
            }
        ],
    }


def _controller_payload(include_controlling=False):
    payload = {
        "sjkzr": [
            {
                "SECUCODE": "002558.SZ",
                "SECURITY_CODE": "002558",
                "HOLDER_NAME": "史玉柱",
                "HOLD_RATIO": None,
            }
        ],
        "sdgd": [
            {
                "END_DATE": "2026-03-31 00:00:00",
                "HOLDER_RANK": 1,
                "HOLDER_NAME": "上海巨人投资管理有限公司",
                "HOLD_NUM_RATIO": 29.69,
            }
        ],
    }
    if include_controlling:
        payload["kggd"] = [
            {
                "HOLDER_NAME": "上海巨人投资管理有限公司",
                "HOLD_RATIO": 29.69,
                "END_DATE": "2026-03-31 00:00:00",
            }
        ]
    return payload


def test_latest_dated_row_and_float_semantics():
    value = ownership.normalize_share_structure(
        _payload(),
        "https://example.invalid?code=SZ002558",
        "SZ002558",
        "2026-08-12T05:00:00+08:00",
    )
    assert value["status"] == "OK"
    assert value["as_of_date"] == "2026-08-01"
    values = value["values"]
    assert values["total_shares"] == 1200.0
    assert values["float_shares"] == 850.0
    assert values["float_scope"] == "LISTED_A_SHARES"
    assert values["free_float_shares"] == 700.0
    assert values["float_ratio_percent"] == 70.8333
    assert values["restricted_ratio_percent"] == 25.0
    assert value["provenance"]["field_mapping"]["float_shares"] == "LISTED_A_SHARES"
    assert value["provenance"]["raw_section"] == "lngbbd"


def test_undated_provider_row_is_not_promoted_to_current_fact():
    payload = {
        "gbjg": [{"TOTAL_SHARES": 1000, "LISTED_A_SHARES": 800, "LIMITED_SHARES": 200}]
    }
    value = ownership.normalize_share_structure(
        payload,
        "https://example.invalid",
        "SZ002558",
        "2026-08-12T05:00:00+08:00",
    )
    assert value["status"] == "UNAVAILABLE"
    assert value["as_of_date"] is None
    assert "NO_DATED_SHARE_STRUCTURE_ROW" in value["metadata"]["quality_flags"]


def test_missing_required_field_is_explicitly_partial():
    payload = {
        "lngbbd": [{"END_DATE": "2026-08-01", "TOTAL_SHARES": 1200, "LISTED_A_SHARES": 850}]
    }
    value = ownership.normalize_share_structure(
        payload,
        "https://example.invalid",
        "SZ002558",
        "2026-08-12T05:00:00+08:00",
    )
    assert value["status"] == "PARTIAL"
    assert value["values"]["restricted_shares"] is None
    assert "MISSING_RESTRICTED_SHARES" in value["metadata"]["quality_flags"]


def test_actual_controller_is_preserved_without_guessing_controlling_shareholder():
    value = ownership.normalize_controllers(
        _controller_payload(),
        "https://example.invalid/shareholders?code=SZ002558",
        "SZ002558",
        "2026-08-12T06:00:00+08:00",
    )
    assert value["status"] == "PARTIAL"
    assert value["actual_controller"]["holders"] == [
        {"name": "史玉柱", "hold_ratio_percent": None, "as_of_date": None}
    ]
    assert value["actual_controller"]["as_of_date"] is None
    assert value["controlling_shareholder"]["status"] == "UNAVAILABLE"
    assert value["controlling_shareholder"]["holders"] == []
    assert (
        value["controlling_shareholder"]["inference_policy"]
        == "PROVIDER_DECLARED_ONLY; NEVER_INFER_FROM_LARGEST_HOLDER"
    )
    assert "ACTUAL_CONTROLLER_UNDATED" in value["metadata"]["quality_flags"]
    assert (
        "CONTROLLING_SHAREHOLDER_NOT_IDENTIFIED_BY_PROVIDER"
        in value["metadata"]["quality_flags"]
    )
    assert "sdgd" not in value["provenance"]["field_mapping"]["controlling_shareholder"]


def test_provider_declared_controlling_shareholder_is_accepted_with_date():
    value = ownership.normalize_controllers(
        _controller_payload(include_controlling=True),
        "https://example.invalid/shareholders?code=SZ002558",
        "SZ002558",
        "2026-08-12T06:00:00+08:00",
    )
    assert value["status"] == "OK"
    assert value["controlling_shareholder"]["status"] == "OK"
    assert value["controlling_shareholder"]["as_of_date"] == "2026-03-31"
    assert value["controlling_shareholder"]["holders"][0]["name"] == "上海巨人投资管理有限公司"
    assert value["controlling_shareholder"]["holders"][0]["hold_ratio_percent"] == 29.69
    assert "ACTUAL_CONTROLLER_UNDATED" in value["metadata"]["quality_flags"]


def test_full_finalize_attaches_share_structure_controllers_and_schema():
    class Base:
        calls = []

        @staticmethod
        def infer_identifiers(code):
            assert code == "002558"
            return "SZ", "0.002558", "sz002558"

        @staticmethod
        def http_get(url):
            Base.calls.append(url)
            assert "code=SZ002558" in url
            if "CapitalStockStructure" in url:
                return json.dumps(_payload())
            if "ShareholderResearch" in url:
                return json.dumps(_controller_payload())
            raise AssertionError(url)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 16,
                    "runner_time_utc": "2026-08-11T22:00:00+00:00",
                    "detail_stocks": {"002558": {"status": "OK"}},
                }
            ),
            encoding="utf-8",
        )
        ownership.finalize_snapshot(path, Base, "FULL")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        assert len(Base.calls) == 2
        context = snapshot["detail_stocks"]["002558"]["ownership_and_capital"]
        assert context["share_structure"]["as_of_date"] == "2026-08-01"
        assert context["controllers"]["actual_controller"]["holders"][0]["name"] == "史玉柱"
        assert context["controllers"]["controlling_shareholder"]["status"] == "UNAVAILABLE"
        assert context["status"] == "PARTIAL"
        assert snapshot["schema_version"] == 17
        assert snapshot["features"]["ownership_and_capital"] == "v1"
        assert snapshot["ownership_and_capital_summary"]["status"] == "PARTIAL"
        assert snapshot["ownership_and_capital_summary"]["implemented_sections"] == [
            "share_structure",
            "controllers",
        ]


def test_intraday_fast_is_network_free_and_explicitly_deferred():
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
                    "runner_time_cst": "2026-08-12 06:00:00",
                    "detail_stocks": {"002558": {"status": "OK"}},
                }
            ),
            encoding="utf-8",
        )
        ownership.finalize_snapshot(path, Base, "INTRADAY_FAST")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        context = snapshot["detail_stocks"]["002558"]["ownership_and_capital"]
        assert context["status"] == "DEFERRED"
        assert context["share_structure"]["metadata"]["freshness"] == "NOT_FETCHED_IN_INTRADAY_FAST"
        assert context["controllers"]["status"] == "DEFERRED"
        assert context["controllers"]["metadata"]["freshness"] == "NOT_FETCHED_IN_INTRADAY_FAST"
        assert snapshot["ownership_and_capital_summary"]["status"] == "DEFERRED"


def main():
    tests = [
        test_latest_dated_row_and_float_semantics,
        test_undated_provider_row_is_not_promoted_to_current_fact,
        test_missing_required_field_is_explicitly_partial,
        test_actual_controller_is_preserved_without_guessing_controlling_shareholder,
        test_provider_declared_controlling_shareholder_is_accepted_with_date,
        test_full_finalize_attaches_share_structure_controllers_and_schema,
        test_intraday_fast_is_network_free_and_explicitly_deferred,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OWNERSHIP_CAPITAL_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
