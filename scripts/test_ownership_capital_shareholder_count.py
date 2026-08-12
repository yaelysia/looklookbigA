import json
import tempfile
from pathlib import Path

import ownership_capital_shareholder_count as shareholder_count


def _payload():
    return {
        "gdrs": [
            {"END_DATE": "2026-06-30", "HOLDER_TOTAL_NUM": 120, "TOTAL_NUM_RATIO": 20.0, "AVG_FREE_SHARES": 800, "AVG_FREESHARES_RATIO": -16.0, "HOLD_FOCUS": "较分散", "PRICE": 30, "AVG_HOLD_AMT": 24000},
            {"END_DATE": "2026-03-31", "HOLDER_TOTAL_NUM": 100, "TOTAL_NUM_RATIO": 25.0, "AVG_FREE_SHARES": 960},
            {"END_DATE": "2025-12-31", "HOLDER_TOTAL_NUM": 80, "TOTAL_NUM_RATIO": -20.0, "AVG_FREE_SHARES": 1200},
            {"END_DATE": "2025-09-30", "HOLDER_TOTAL_NUM": 100, "TOTAL_NUM_RATIO": 0.0, "AVG_FREE_SHARES": 960},
            {"END_DATE": "2025-06-30", "HOLDER_TOTAL_NUM": 100, "AVG_FREE_SHARES": 960},
        ]
    }


def test_history_period_delta_and_3_6_12m_windows():
    value = shareholder_count.normalize_shareholder_count(
        _payload(), "https://example.invalid/shareholder-count", "SZ002558", "2026-08-12T09:00:00+08:00"
    )
    assert value["status"] == "OK"
    assert value["as_of_date"] == "2026-06-30"
    assert value["latest"]["shareholder_count"] == 120
    assert value["latest"]["average_free_shares"] == 800.0
    assert value["latest"]["change_from_previous"]["shareholder_count_delta"] == 20
    assert value["window_trends"]["3m"]["baseline_report_date"] == "2026-03-31"
    assert value["window_trends"]["6m"]["baseline_report_date"] == "2025-12-31"
    assert value["window_trends"]["12m"]["baseline_report_date"] == "2025-06-30"
    assert value["trend"] == "SHAREHOLDER_COUNT_RISING"
    assert value["metadata"]["realtime"] is False
    assert value["metadata"]["disclosure_lag"] is True


def test_mixed_window_directions_are_structural_volatility_not_market_judgment():
    payload = {"gdrs": [
        {"END_DATE": "2026-06-30", "HOLDER_TOTAL_NUM": 100},
        {"END_DATE": "2026-03-31", "HOLDER_TOTAL_NUM": 120},
        {"END_DATE": "2025-12-31", "HOLDER_TOTAL_NUM": 80},
        {"END_DATE": "2025-06-30", "HOLDER_TOTAL_NUM": 110},
    ]}
    value = shareholder_count.normalize_shareholder_count(
        payload, "https://example.invalid/shareholder-count", "SZ002558", "2026-08-12T09:00:00+08:00"
    )
    assert value["trend"] == "VOLATILE"


def test_missing_provider_section_fails_closed_without_fabricated_count():
    value = shareholder_count.normalize_shareholder_count(
        {"unexpected": []}, "https://example.invalid/shareholder-count", "SZ002558", "2026-08-12T09:00:00+08:00"
    )
    assert value["status"] == "UNAVAILABLE"
    assert value["latest"] is None
    assert value["trend"] == "UNKNOWN"
    assert "SHAREHOLDER_COUNT_SECTION_NOT_EXPOSED" in value["metadata"]["quality_flags"]


def test_full_extension_fetches_one_shareholder_research_payload_and_attaches_history():
    class Base:
        calls = []

        @staticmethod
        def infer_identifiers(code):
            assert code == "002558"
            return "SZ", "0.002558", "sz002558"

        @staticmethod
        def http_get(url):
            Base.calls.append(url)
            assert "ShareholderResearch/PageAjax" in url
            assert "code=SZ002558" in url
            return json.dumps(_payload())

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(json.dumps({
            "runner_time_utc": "2026-08-12T01:00:00+00:00",
            "detail_stocks": {"002558": {"ownership_and_capital": {
                "share_structure": {"status": "OK"}, "controllers": {"status": "OK"},
                "top_holders": {"status": "OK"}, "institutional_holdings": {"status": "OK"},
                "status": "OK"
            }}},
            "ownership_and_capital_summary": {"implemented_sections": [
                "share_structure", "controllers", "top_holders", "institutional_holdings"
            ]}
        }), encoding="utf-8")
        shareholder_count.extend_snapshot(path, Base, "FULL")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        context = snapshot["detail_stocks"]["002558"]["ownership_and_capital"]
        assert len(Base.calls) == 1
        assert context["shareholder_count"]["latest"]["shareholder_count"] == 120
        assert context["status"] == "OK"
        assert snapshot["ownership_and_capital_summary"]["implemented_sections"][-1] == "shareholder_count"


def test_intraday_fast_defers_shareholder_count_without_network():
    class Base:
        @staticmethod
        def infer_identifiers(code):
            raise AssertionError("FAST path must not resolve provider identifiers")

        @staticmethod
        def http_get(url):
            raise AssertionError("FAST path must not issue ownership network requests")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(json.dumps({
            "runner_time_utc": "2026-08-12T01:00:00+00:00",
            "detail_stocks": {"002558": {"ownership_and_capital": {
                "share_structure": {"status": "DEFERRED"}, "controllers": {"status": "DEFERRED"},
                "top_holders": {"status": "DEFERRED"}, "institutional_holdings": {"status": "DEFERRED"},
                "status": "DEFERRED"
            }}},
            "ownership_and_capital_summary": {"implemented_sections": [
                "share_structure", "controllers", "top_holders", "institutional_holdings"
            ]}
        }), encoding="utf-8")
        shareholder_count.extend_snapshot(path, Base, "INTRADAY_FAST")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        context = snapshot["detail_stocks"]["002558"]["ownership_and_capital"]
        assert context["shareholder_count"]["status"] == "DEFERRED"
        assert context["status"] == "DEFERRED"


def main():
    tests = [
        test_history_period_delta_and_3_6_12m_windows,
        test_mixed_window_directions_are_structural_volatility_not_market_judgment,
        test_missing_provider_section_fails_closed_without_fabricated_count,
        test_full_extension_fetches_one_shareholder_research_payload_and_attaches_history,
        test_intraday_fast_defers_shareholder_count_without_network,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OWNERSHIP_SHAREHOLDER_COUNT_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
