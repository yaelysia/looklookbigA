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


def test_full_finalize_attaches_share_structure_and_schema():
    class Base:
        calls = 0

        @staticmethod
        def infer_identifiers(code):
            assert code == "002558"
            return "SZ", "0.002558", "sz002558"

        @staticmethod
        def http_get(url):
            Base.calls += 1
            assert "code=SZ002558" in url
            return json.dumps(_payload())

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 16,
                    "runner_time_utc": "2026-08-11T21:00:00+00:00",
                    "detail_stocks": {"002558": {"status": "OK"}},
                }
            ),
            encoding="utf-8",
        )
        ownership.finalize_snapshot(path, Base, "FULL")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        assert Base.calls == 1
        context = snapshot["detail_stocks"]["002558"]["ownership_and_capital"]
        assert context["share_structure"]["as_of_date"] == "2026-08-01"
        assert snapshot["schema_version"] == 17
        assert snapshot["features"]["ownership_and_capital"] == "v1"
        assert snapshot["ownership_and_capital_summary"]["status"] == "OK"


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
                    "runner_time_cst": "2026-08-12 05:00:00",
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
        assert snapshot["ownership_and_capital_summary"]["status"] == "DEFERRED"


def main():
    tests = [
        test_latest_dated_row_and_float_semantics,
        test_undated_provider_row_is_not_promoted_to_current_fact,
        test_missing_required_field_is_explicitly_partial,
        test_full_finalize_attaches_share_structure_and_schema,
        test_intraday_fast_is_network_free_and_explicitly_deferred,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OWNERSHIP_CAPITAL_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
