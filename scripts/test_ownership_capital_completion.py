import copy
import json
import tempfile
from datetime import datetime
from pathlib import Path

import changes_since_previous
import company_events
import company_event_facts
import history_store
import ownership_capital_analysis as analysis
import ownership_capital_base as core
import ownership_capital_tests_legacy as legacy
import realtime_quotes_watchlist as watchlist
import test_ownership_capital
import test_ownership_capital_event_state
import test_ownership_capital_shareholder_count


def _coverage():
    return {
        "covered_start_date": "2026-04-01",
        "coverage_days": 133,
        "coverage_complete": True,
        "target_history_days": 90,
        "sufficient_for_persistent_state": True,
        "source": "CNINFO_EVENT_CACHE",
    }


def _complete_context():
    share_values = {
        "total_shares": 1000.0,
        "float_shares": 800.0,
        "restricted_shares": 200.0,
        "float_scope": "LISTED_A_SHARES",
    }
    share_history = [
        {
            "as_of_date": "2026-06-30",
            "values": share_values,
            "change_from_previous": {
                "previous_as_of_date": "2026-03-31",
                "total_shares": {"delta": 0.0, "change_percent": 0.0, "comparable": True},
                "float_shares": {"delta": 0.0, "change_percent": 0.0, "comparable": True},
                "restricted_shares": {"delta": 0.0, "change_percent": 0.0, "comparable": True},
            },
        },
        {"as_of_date": "2026-03-31", "values": share_values, "change_from_previous": None},
    ]
    return {
        "version": "v1",
        "status": "OK",
        "share_structure": {
            "status": "OK", "as_of_date": "2026-06-30", "values": share_values,
            "history": share_history, "trend": {"state": "STABLE"},
            "metadata": {"freshness": "LATEST_DISCLOSED_SHARE_STRUCTURE", "quality": "PASS", "quality_flags": []},
            "provenance": {"provider": "Eastmoney", "source_tier": "PRIMARY_PROVIDER"},
        },
        "controllers": {
            "status": "OK", "as_of_date": "2026-06-30",
            "actual_controller": {"status": "OK", "holders": [{"name": "实控甲"}]},
            "controlling_shareholder": {"status": "OK", "holders": [{"name": "控股甲"}]},
            "control_change": {"state": "UNCHANGED"}, "recent_holding_changes": [],
            "metadata": {"freshness": "CURRENT_PROVIDER_RELATIONSHIP", "quality": "PASS", "quality_flags": []},
            "provenance": {"provider": "Eastmoney", "source_tier": "PRIMARY_PROVIDER"},
        },
        "top_holders": {
            "status": "OK", "as_of_date": "2026-06-30",
            "top_shareholders": {"latest": {"report_date": "2026-06-30", "holders": [
                {"name": "控股甲", "change_shares": 10, "change_ratio_percent": 1.0,
                 "hold_ratio_change_percent": 1.0, "change_state": "增加"}
            ]}},
            "concentration_trend": {"state": "RISING", "change_pp": 2.0,
                                      "latest_report_date": "2026-06-30", "baseline_report_date": "2026-03-31"},
            "metadata": {"freshness": "REPORT_PERIOD_HISTORY", "quality": "PASS", "quality_flags": []},
            "provenance": {"provider": "Eastmoney", "source_tier": "PRIMARY_PROVIDER"},
        },
        "institutional_holdings": {
            "status": "OK", "as_of_date": "2026-06-30", "trend": {"state": "RISING", "hold_ratio_change_pp": 1.0},
            "metadata": {"freshness": "REPORT_PERIOD_HISTORY", "quality": "PASS", "quality_flags": []},
            "provenance": {"provider": "Eastmoney", "source_tier": "PRIMARY_PROVIDER"},
        },
        "shareholder_count": {
            "status": "OK", "as_of_date": "2026-06-30", "trend": "SHAREHOLDER_COUNT_RISING",
            "window_trends": {"3m": {"shareholder_count_change_percent": 10.0}},
            "metadata": {"freshness": "DISCLOSED_SHAREHOLDER_COUNT_HISTORY", "quality": "PASS", "quality_flags": []},
            "provenance": {"provider": "Eastmoney", "source_tier": "PRIMARY_PROVIDER"},
        },
        "buyback_and_holder_plans": {
            "status": "OK", "as_of_date": "2026-08-12",
            "buybacks": {"confirmed_active": [{"event_id": "buy-1", "active_execution_window": "ACTIVE"}]},
            "holder_increase_plans": {"current": None}, "holder_decrease_plans": {"current": None},
            "metadata": {"freshness": "OFFICIAL_EVENT_DERIVED_STATE", "quality": "PASS", "quality_flags": []},
            "provenance": {"provider": "CNINFO", "source_tier": "OFFICIAL"},
        },
        "unlocks": {
            "status": "OK", "as_of_date": "2026-08-12",
            "upcoming": [{"event_id": "unlock-1", "unlock_date": "2026-09-01", "days_until": 20,
                          "status": "OPEN", "title": "限售股上市", "importance": "MEDIUM",
                          "unlock_shares": 60, "unlock_ratio_total_percent": 6.0,
                          "unlock_ratio_float_percent": 7.5}],
            "metadata": {"freshness": "OFFICIAL_EVENT_DERIVED_STATE", "quality": "PASS", "quality_flags": []},
            "provenance": {"provider": "CNINFO", "source_tier": "OFFICIAL"},
        },
    }


def test_share_structure_history_and_structural_trend():
    value = core.normalize_share_structure(
        legacy._payload(), "https://example.invalid", "SZ002558", "2026-08-12T10:00:00+08:00"
    )
    assert [item["as_of_date"] for item in value["history"]] == ["2026-08-01", "2026-06-30"]
    assert value["history"][0]["change_from_previous"]["total_shares"]["delta"] == 200.0
    assert value["trend"]["state"] == "RISING"
    assert value["trend"]["total_shares"]["change_percent"] == 20.0


def test_controller_change_and_concert_party_are_explicit_only():
    payload = {
        "sjkzr": [
            {"HOLDER_NAME": "新实控", "HOLD_RATIO": 30, "END_DATE": "2026-06-30"},
            {"HOLDER_NAME": "旧实控", "HOLD_RATIO": 28, "END_DATE": "2026-03-31"},
        ],
        "kggd": [
            {"HOLDER_NAME": "控股甲", "HOLD_RATIO": 20, "END_DATE": "2026-06-30",
             "RELATION_TYPE": "一致行动人"},
            {"HOLDER_NAME": "控股乙", "HOLD_RATIO": 10, "END_DATE": "2026-06-30",
             "IS_CONCERT_PARTY": 1},
        ],
    }
    value = core.normalize_controllers(
        payload, "https://example.invalid", "SZ002558", "2026-08-12T10:00:00+08:00"
    )
    assert value["control_change"]["state"] == "CHANGED"
    assert value["concert_party_aggregate"]["aggregate_hold_ratio_percent"] == 30.0
    assert "NEVER_INFER" in value["concert_party_aggregate"]["evidence_policy"]


def test_capital_tools_fail_closed_on_unscoped_percentages():
    events = [
        {
            "event_id": "pledge-1", "event_type": "PLEDGE", "status": "OPEN",
            "published_at": "2026-08-10T09:00:00+08:00", "title": "股份质押公告",
            "source_tier": "OFFICIAL", "source_document_id": "pledge-1",
            "source_url": "https://static.cninfo.com.cn/pledge-1.pdf",
            "facts": {"pledge_percentages": [12.0], "extraction_scope": "TITLE_ONLY"},
        },
        {
            "event_id": "refi-1", "event_type": "REFINANCING", "status": "OPEN",
            "published_at": "2026-08-09T09:00:00+08:00", "title": "向特定对象发行公告",
            "source_tier": "OFFICIAL", "facts": {"potential_dilution_percent": 8.0},
        },
    ]
    value = analysis.normalize_capital_tools(
        events, _coverage(), "2026-08-12", "2026-08-12T10:00:00+08:00"
    )
    assert value["overall_pledge_ratio_percent"] is None
    assert value["pledges"][0]["provider_unscoped_percentages"] == [12.0]
    assert "PERCENTAGE_SCOPE_UNAVAILABLE" in value["pledges"][0]["quality_flags"]
    assert value["refinancing"][0]["potential_dilution_percent"] == 8.0
    assert company_events.classify_event("关于股份质押的公告") == "PLEDGE"
    assert company_events.classify_event("向特定对象发行股票预案") == "REFINANCING"
    assert company_events.classify_event("公开发行可转换公司债券公告") == "CONVERTIBLE_BOND"
    assert company_events.classify_event("优先股发行预案") == "PREFERRED_SHARES"
    assert set(analysis.CAPITAL_EVENT_TYPES) <= company_event_facts.FACT_ENRICH_TYPES


def test_signals_upcoming_valuation_and_unified_metadata_integration():
    old_path = company_events._event_cache_path
    old_read = company_events._read_json
    try:
        company_events._event_cache_path = lambda code: Path(f"/virtual/{code}.json")
        company_events._read_json = lambda path: {
            "source": "CNINFO", "code": "002558", "covered_start_date": "2026-04-01",
            "query_status": "OK", "events": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            context = _complete_context()
            path.write_text(json.dumps({
                "runner_time_cst": "2026-08-12T10:00:00+08:00",
                "detail_stocks": {"002558": {
                    "quote": {"total_market_cap": 30000, "float_market_cap": 24000},
                    "events": {"cache": {"coverage_complete": True, "covered_start_date": "2026-04-01"}},
                    "ownership_and_capital": context,
                }},
                "ownership_and_capital_summary": {"status": "OK", "implemented_sections": []},
            }), encoding="utf-8")
            analysis.extend_snapshot(path, object(), "FULL")
            stock = json.loads(path.read_text(encoding="utf-8"))["detail_stocks"]["002558"]
            result = stock["ownership_and_capital"]
            signals = {item["signal"] for item in result["structural_signals"]["signals"]}
            assert {
                "CONTROLLING_HOLDER_INCREASING", "OWNERSHIP_CONCENTRATION_RISING",
                "INSTITUTIONAL_HOLDING_RISING", "SHAREHOLDER_COUNT_RISING",
                "BUYBACK_IN_PROGRESS", "MAJOR_UNLOCK_UPCOMING", "CAPITAL_STRUCTURE_STABLE",
            } <= signals
            assert stock["upcoming_events"]["events"][0]["event_id"] == "unlock-1"
            assert result["valuation_share_semantics"]["total_market_cap"]["scope"] == "TOTAL_SHARES"
            assert result["share_structure"]["metadata"]["freshness_policy"]
            assert result["metadata"]["source_tier"] == "INHERITED"
            assert result["provenance"]["algorithm"] == "ownership_and_capital_v1"
    finally:
        company_events._event_cache_path = old_path
        company_events._read_json = old_read


def test_changes_only_report_new_disclosures_or_persistent_state_changes():
    before = {"ownership_and_capital": _complete_context()}
    after = copy.deepcopy(before)
    after["ownership_and_capital"]["share_structure"]["metadata"]["fetched_at"] = "later"
    unchanged = changes_since_previous._ownership_changes(before, after)
    assert unchanged["changed"] is False

    after["ownership_and_capital"]["shareholder_count"]["as_of_date"] = "2026-07-31"
    after["ownership_and_capital"]["shareholder_count"]["latest"] = {"shareholder_count": 130}
    changed = changes_since_previous._ownership_changes(before, after)
    assert changed["changed"] is True
    assert changed["new_disclosures"][0]["section"] == "shareholder_count"

    no_baseline = changes_since_previous._ownership_changes({}, after)
    assert no_baseline["status"] == "NO_COMPARABLE_BASELINE"
    assert no_baseline["changed"] is False

    deferred = copy.deepcopy(before)
    for name in (
        "share_structure", "controllers", "top_holders", "institutional_holdings",
        "shareholder_count", "buyback_and_holder_plans", "unlocks",
    ):
        deferred["ownership_and_capital"][name] = {"status": "DEFERRED"}
    deferred_change = changes_since_previous._ownership_changes(before, deferred)
    assert deferred_change["changed"] is False


def test_exact_history_keeps_ownership_and_shared_upcoming_events():
    snapshot = {
        "detail_stocks": {"002558": {
            "ownership_and_capital": _complete_context(),
            "upcoming_events": {"status": "OK", "events": [{"event_id": "unlock-1"}]},
        }}
    }
    compact = history_store._compact_snapshot(snapshot)
    assert compact["detail_stocks"]["002558"]["ownership_and_capital"]["version"] == "v1"
    assert compact["detail_stocks"]["002558"]["upcoming_events"]["events"][0]["event_id"] == "unlock-1"


def test_watchlist_quote_exposes_market_cap_scope_inputs():
    old = watchlist.eastmoney_quote
    try:
        watchlist.eastmoney_quote = lambda secid: {
            "f20": 30000, "f21": 24000, "f43": 30, "f48": 100,
            "f57": "002558", "f58": "测试", "f86": 1786500000,
        }
        quote = watchlist.quote_payload(datetime(2026, 8, 12, 10, 0, tzinfo=watchlist.CST), "002558")
        assert quote["total_market_cap"] == 30000
        assert quote["float_market_cap"] == 24000
        assert "f20" in watchlist.QUOTE_FIELDS and "f21" in watchlist.QUOTE_FIELDS
    finally:
        watchlist.eastmoney_quote = old


def test_required_workflow_catalog_has_one_completion_entrypoint():
    workflows = (
        ".github/workflows/pre-merge-security-gate.yml",
        ".github/workflows/reusable-selftest.yml",
        ".github/workflows/v1-smoke.yml",
    )
    command = "python3 scripts/test_ownership_capital_completion.py"
    for name in workflows:
        text = Path(name).read_text(encoding="utf-8")
        assert text.count(command) == 1, name
    realtime = Path(".github/workflows/realtime-quotes.yml").read_text(encoding="utf-8")
    for name in (
        "scripts/ownership_capital_analysis.py",
        "scripts/test_ownership_capital_completion.py",
    ):
        assert f'"{name}"' in realtime


def main():
    test_ownership_capital.main()
    test_ownership_capital_shareholder_count.main()
    test_ownership_capital_event_state.main()
    tests = [
        test_share_structure_history_and_structural_trend,
        test_controller_change_and_concert_party_are_explicit_only,
        test_capital_tools_fail_closed_on_unscoped_percentages,
        test_signals_upcoming_valuation_and_unified_metadata_integration,
        test_changes_only_report_new_disclosures_or_persistent_state_changes,
        test_exact_history_keeps_ownership_and_shared_upcoming_events,
        test_watchlist_quote_exposes_market_cap_scope_inputs,
        test_required_workflow_catalog_has_one_completion_entrypoint,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OWNERSHIP_CAPITAL_COMPLETION_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
