import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import capital_flow_changes
import capital_flow_context as capital
import capital_flow_history_bridge
import capital_flow_review_hardening as hardening
import capital_flow_window_bridge
import data_metadata
import data_policy_bridge
import history_store


data_policy_bridge.install(data_metadata)
capital_flow_window_bridge.install(capital)
capital_flow_history_bridge.install(history_store)
CST = timezone(timedelta(hours=8))


def _quote(amount, latest=10.0, when="2026-08-10 10:30:00"):
    return {
        "amount_raw": float(amount),
        "latest": float(latest),
        "market_time_cst": when,
    }


def _groups(codes):
    return {
        "sector": {
            "target": {"code": "002558"},
            "members": [{"code": code} for code in codes],
        }
    }


def test_compact_history_persists_minimum_light_peer_quote():
    data = {
        "schema_version": 14,
        "runner_time_cst": "2026-08-10 10:30:00",
        "detail_stocks": {
            "002558": {
                "code": "002558",
                "quote": _quote(1000, 10.0),
                "capital_flow": {"status": "OK"},
            }
        },
        "light_stocks": {
            "000001": {"code": "000001", "quote": {**_quote(500, 9.0), "extra": "drop-me"}},
        },
        "groups": _groups(["000001"]),
    }
    compact = history_store._compact_snapshot(data)
    assert compact["light_stocks"]["000001"]["quote"] == {
        "latest": 9.0,
        "amount_raw": 500.0,
        "market_time_cst": "2026-08-10 10:30:00",
    }
    assert compact["detail_stocks"]["002558"]["capital_flow"]["status"] == "OK"


def test_single_available_peer_cannot_manufacture_strong_sync_or_rank():
    peers = [f"P{i:02d}" for i in range(12)]
    previous = {
        "runner_time_cst": "2026-08-10 10:25:00",
        "detail_stocks": {"002558": {"quote": _quote(900, 9.9)}},
        "light_stocks": {peers[0]: {"quote": _quote(400, 9.9)}},
        "groups": _groups(peers),
    }
    current = {
        "runner_time_cst": "2026-08-10 10:30:00",
        "detail_stocks": {"002558": {"quote": _quote(1100, 10.1)}},
        "light_stocks": {peers[0]: {"quote": _quote(500, 10.1)}},
        "groups": _groups(peers),
    }
    value = capital._peer_context(current, previous, "002558")
    primary = value["primary"]
    assert primary["requested_peer_count"] == 12
    assert primary["available_peer_count"] == 1
    assert primary["peer_count"] == 1
    assert primary["target_included_in_peer_count"] is False
    assert primary["peer_coverage"] < primary["minimum_required_coverage"]
    assert primary["status"] == "PARTIAL"
    assert value["status"] == "PARTIAL"
    assert primary["relative_capital_strength"] is None
    assert primary["rank"] is None
    assert primary["sector_sync"] == "UNKNOWN"


def test_available_peer_set_change_is_noncomparable_even_when_config_is_same():
    peers = [f"P{i:02d}" for i in range(5)]
    previous_available = peers[:3]
    current_available = peers[1:4]
    previous = {
        "runner_time_cst": "2026-08-10 10:25:00",
        "detail_stocks": {
            "002558": {
                "quote": _quote(900, 9.9),
                "capital_flow": {
                    "peer_context": {
                        "primary": {
                            "available_peer_signature": "|".join(sorted(previous_available)),
                        }
                    }
                },
            }
        },
        "light_stocks": {code: {"quote": _quote(400 + i * 10, 9.9)} for i, code in enumerate(current_available)},
        "groups": _groups(peers),
    }
    current = {
        "runner_time_cst": "2026-08-10 10:30:00",
        "detail_stocks": {"002558": {"quote": _quote(1100, 10.1)}},
        "light_stocks": {code: {"quote": _quote(500 + i * 10, 10.1)} for i, code in enumerate(current_available)},
        "groups": _groups(peers),
    }
    primary = capital._peer_context(current, previous, "002558")["primary"]
    assert primary["status"] == "OK"
    assert primary["comparability"]["configured_universe_same"] is True
    assert primary["available_peer_signature"] != primary["previous_available_peer_signature"]
    assert primary["comparability"]["comparable_to_previous"] is False


def test_margin_provider_regression_preserves_newer_cache_and_is_noncomparable():
    class Base:
        pass

    now = datetime(2026, 8, 10, 16, 0, tzinfo=CST)
    old_root = os.environ.get("MARKET_HISTORY_DIR")
    old_fetch = capital.fetch_margin_history
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_HISTORY_DIR"] = tmp
            cache_path = capital._margin_cache_path("002558")
            capital._write_json(cache_path, {
                "schema_version": 1,
                "code": "002558",
                "source": "Eastmoney",
                "source_tier": "PRIMARY_PROVIDER",
                "fetched_at": "2026-08-08T18:00:00+08:00",
                "source_url": "https://example.invalid/cache",
                "records": [
                    {"trade_date": "2026-08-07", "financing_balance": 1000.0},
                    {"trade_date": "2026-08-06", "financing_balance": 950.0},
                ],
            })

            def regressed_fetch(base, code, limit=24):
                return [
                    {"trade_date": "2026-08-06", "financing_balance": 900.0},
                    {"trade_date": "2026-08-05", "financing_balance": 880.0},
                ], "https://example.invalid/provider"

            capital.fetch_margin_history = regressed_fetch
            after_margin = capital._margin_context(Base, "002558", now, "FULL", {})
            persisted = json.loads(cache_path.read_text(encoding="utf-8"))
            assert persisted["records"][0]["trade_date"] == "2026-08-07"
            assert persisted["records"][0]["financing_balance"] == 1000.0
            assert after_margin["as_of_trade_date"] == "2026-08-07"
            assert after_margin["provider_session_regressed"] is True
            assert after_margin["provider_returned_trade_date"] == "2026-08-06"
            assert after_margin["session_guard"]["reason"] == "MARGIN_SESSION_REGRESSED"

            before = {
                "observed": {"turnover": {"amount_rate_5m": 100, "amount_rate_vs_baseline": 1.0}},
                "derived": {
                    "pressure": {"net_bias": "BALANCED"},
                    "absorption": {"state": "NONE"},
                    "price_volume_confirmation": {"state": "NEUTRAL"},
                    "vwap_acceptance": {"state": "OSCILLATING_AROUND_VWAP"},
                },
                "peer_context": {"primary": {}},
                "official_delayed": {"margin": {"as_of_trade_date": "2026-08-07", "financing_balance": 1000.0}},
            }
            after = json.loads(json.dumps(before))
            after["official_delayed"]["margin"] = after_margin
            change = capital_flow_changes.build_change(before, after)
            assert change["margin"]["session_state"] == "REGRESSED"
            assert change["margin"]["new_disclosed_session"] is False
            assert change["margin"]["financing_balance"]["delta"] is None
            assert change["margin"]["financing_balance"]["comparable"] is False
            assert "MARGIN_SESSION_REGRESSED" in change["reason_codes"]
            assert "NEW_MARGIN_DISCLOSURE" not in change["reason_codes"]
    finally:
        capital.fetch_margin_history = old_fetch
        if old_root is None:
            os.environ.pop("MARKET_HISTORY_DIR", None)
        else:
            os.environ["MARKET_HISTORY_DIR"] = old_root


def test_missing_financing_balance_is_not_a_zero_observation():
    assert capital._normalize_margin_row({"DATE": "2026-08-07", "RZMRE": 10}) is None
    records = [
        {"trade_date": "2026-08-07", "financing_balance": 1000.0},
        {"trade_date": "2026-08-06", "financing_balance": None},
    ]
    assert capital._margin_change(records, 1) is None


def test_cross_session_previous_rate_is_explicitly_noncomparable():
    value = {
        "observed": {
            "turnover": {
                "amount_rate_5m": 120.0,
                "amount_rate_vs_previous_snapshot": 1.5,
                "previous_snapshot_comparable": True,
            }
        }
    }
    current_item = {"quote": _quote(1000, 10.0, "2026-08-10 09:35:00")}
    previous = {
        "detail_stocks": {
            "002558": {
                "quote": _quote(900, 9.9, "2026-08-07 14:55:00"),
            }
        }
    }
    hardening._session_guard_turnover(capital, value, "002558", current_item, previous)
    turnover = value["observed"]["turnover"]
    assert turnover["previous_snapshot_comparable"] is False
    assert turnover["amount_rate_vs_previous_snapshot"] is None
    assert turnover["previous_snapshot_comparability"]["reason"] == "MARKET_SESSION_RESET"


def test_generic_summary_is_recounted_after_capital_flow_severity_upgrade():
    changes = {
        "stocks": {
            "002558": {"significance": "MODERATE"},
            "600795": {"significance": "NONE"},
        },
        "groups": {},
        "market": {"significance": "NONE"},
        "events": {"significance": "NONE"},
        "summary": {"significant_changes": 0, "moderate_changes": 0, "minor_changes": 0},
    }
    hardening._recount_generic_summary(changes)
    assert changes["summary"]["moderate_changes"] == 1
    assert changes["summary"]["significant_changes"] == 0
    assert changes["summary"]["minor_changes"] == 0


def main():
    tests = [
        test_compact_history_persists_minimum_light_peer_quote,
        test_single_available_peer_cannot_manufacture_strong_sync_or_rank,
        test_available_peer_set_change_is_noncomparable_even_when_config_is_same,
        test_margin_provider_regression_preserves_newer_cache_and_is_noncomparable,
        test_missing_financing_balance_is_not_a_zero_observation,
        test_cross_session_previous_rate_is_explicitly_noncomparable,
        test_generic_summary_is_recounted_after_capital_flow_severity_upgrade,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"CAPITAL_FLOW_REVIEW_HARDENING_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
