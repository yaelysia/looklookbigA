import json
import os
import tempfile
from datetime import datetime, timezone, timedelta

import capital_flow_changes
import capital_flow_context as capital
import capital_flow_window_bridge
import data_metadata
import data_policy_bridge


data_policy_bridge.install(data_metadata)
capital_flow_window_bridge.install(capital)
CST = timezone(timedelta(hours=8))


def _mins(count=40, start_price=10.0):
    out = []
    cum_v = 0.0
    cum_a = 0.0
    for i in range(count):
        # First half gently down, second half recovers with stronger activity.
        if i < count // 2:
            price = start_price - i * 0.01
            delta_amount = 1000.0 + i * 10
        else:
            price = start_price - (count // 2) * 0.01 + (i - count // 2) * 0.018
            delta_amount = 1800.0 + i * 10
        delta_volume = delta_amount / max(price, 0.1)
        cum_v += delta_volume
        cum_a += delta_amount
        out.append({
            "time": f"10{i//60:02d}{i%60:02d}",
            "price": round(price, 4),
            "cum_volume": cum_v,
            "cum_amount": cum_a,
            "delta_volume": delta_volume,
            "delta_amount": delta_amount,
        })
    return out


def test_observed_turnover_and_directional_structure_are_not_vendor_flow():
    mins = _mins()
    turnover = capital._turnover(mins, {"amount_raw": mins[-1]["cum_amount"], "volume_raw": mins[-1]["cum_volume"]})
    structure = capital._volume_structure(mins)
    assert turnover["amount_5m"] is not None
    assert turnover["amount_rate_5m"] is not None
    assert turnover["amount_rate_vs_baseline"] is not None
    assert structure["full_session"]["up_amount"] > 0
    assert structure["full_session"]["down_amount"] > 0
    assert structure["last_30m"]["classified_minutes"] == 30
    assert structure["last_15m"]["classified_minutes"] == 15
    assert "not true active buy/sell" in structure["full_session"]["semantic_note"]


def test_pressure_absorption_and_vwap_are_explainable_derived_metrics():
    mins = _mins()
    turnover = capital._turnover(mins, {})
    structure = capital._volume_structure(mins)
    intraday = {"vwap": 9.9, "price_vs_vwap_percent": 1.0, "trend_15m_percent": 1.2, "above_vwap": True}
    distribution = capital._vwap_distribution(mins, 9.9)
    confirmation = capital._price_volume_confirmation(mins, turnover)
    pressure = capital._pressure(structure, intraday)
    absorption = capital._absorption(mins, intraday)
    acceptance = capital._vwap_acceptance(distribution, intraday, mins)
    assert confirmation["state"] in {
        "UP_VOLUME_EXPANSION", "UP_VOLUME_CONTRACTION", "DOWN_VOLUME_EXPANSION",
        "DOWN_VOLUME_CONTRACTION", "NEUTRAL", "UNKNOWN",
    }
    assert pressure["net_bias"] in {"BUY", "SELL", "BALANCED"}
    assert pressure["formula"]
    assert pressure["evidence"]
    assert "not observed capital net inflow" in pressure["semantic_note"]
    assert absorption["state"] in {"STRONG", "MODERATE", "WEAK", "NONE", "UNKNOWN"}
    assert absorption["evidence"] or absorption["state"] in {"NONE", "UNKNOWN"}
    assert acceptance["state"] in {
        "ACCEPTED_ABOVE_VWAP", "ACCEPTED_BELOW_VWAP", "RECLAIMING_VWAP",
        "REJECTED_AT_VWAP", "OSCILLATING_AROUND_VWAP", "UNKNOWN",
    }


def test_margin_fast_path_is_cache_only_and_truthfully_unverified():
    class Base:
        http_calls = 0

        @staticmethod
        def http_get(url):
            Base.http_calls += 1
            raise AssertionError("FAST must not request margin over network")

    now = datetime(2026, 8, 10, 10, 30, tzinfo=CST)
    old_root = os.environ.get("MARKET_HISTORY_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["MARKET_HISTORY_DIR"] = tmp
        path = capital._margin_cache_path("002558")
        capital._write_json(path, {
            "schema_version": 1,
            "source": "Eastmoney",
            "source_url": "https://example.invalid/provider",
            "fetched_at": "2026-08-09T18:00:00+08:00",
            "records": [
                {"trade_date": "2026-08-07", "financing_balance": 1000.0, "financing_buy_amount": 30.0, "financing_repay_amount": 20.0},
                {"trade_date": "2026-08-06", "financing_balance": 950.0, "financing_buy_amount": 25.0, "financing_repay_amount": 22.0},
            ],
        })
        value = capital._margin_context(Base, "002558", now, "INTRADAY_FAST", {})
        assert Base.http_calls == 0
        assert value["status"] == "CACHED"
        assert value["as_of_trade_date"] == "2026-08-07"
        assert value["cache_only_fast_path"] is True
        _, _, margin_meta, _ = capital._build_metadata(now.isoformat(), {"source": "Tencent", "freshness": "LIVE", "lag_seconds": 10}, value, {"status": "UNAVAILABLE"}, "DEGRADED")
        assert margin_meta["trust"]["tier"] == "B"
        assert margin_meta["freshness_sla"]["status"] == "UNMEASURED"
        assert margin_meta["freshness_sla"]["reason"] == "SESSION_COMPLETENESS_UNVERIFIED"
    if old_root is None:
        os.environ.pop("MARKET_HISTORY_DIR", None)
    else:
        os.environ["MARKET_HISTORY_DIR"] = old_root


def test_margin_normalization_keeps_official_delayed_semantics_separate_from_provider_trust():
    row = capital._normalize_margin_row({
        "TRADE_DATE": "2026-08-07 00:00:00",
        "FIN_BALANCE": "123456",
        "FIN_BUY_AMT": "10000",
        "FIN_REPAY_AMT": "8000",
        "SEC_LENDING_BALANCE": "100",
        "MARGIN_BALANCE": "123556",
    })
    assert row["trade_date"] == "2026-08-07"
    assert row["financing_balance"] == 123456.0
    assert row["financing_net_buy_amount"] == 2000.0


def test_peer_universe_change_blocks_relative_change_comparison():
    before = {
        "observed": {"turnover": {"amount_rate_5m": 100.0, "amount_rate_vs_baseline": 1.0}},
        "derived": {
            "pressure": {"net_bias": "BUY"},
            "absorption": {"state": "WEAK"},
            "price_volume_confirmation": {"state": "UP_VOLUME_EXPANSION"},
            "vwap_acceptance": {"state": "ACCEPTED_ABOVE_VWAP"},
        },
        "peer_context": {"primary": {"peer_universe_signature": "A|B", "relative_capital_strength": 1.1, "rank": 1}},
        "official_delayed": {"margin": {"as_of_trade_date": "2026-08-07", "financing_balance": 1000.0}},
    }
    after = {
        "observed": {"turnover": {"amount_rate_5m": 120.0, "amount_rate_vs_baseline": 1.4}},
        "derived": {
            "pressure": {"net_bias": "BUY"},
            "absorption": {"state": "MODERATE"},
            "price_volume_confirmation": {"state": "UP_VOLUME_EXPANSION"},
            "vwap_acceptance": {"state": "ACCEPTED_ABOVE_VWAP"},
        },
        "peer_context": {"primary": {
            "peer_universe_signature": "A|B|C",
            "previous_peer_universe_signature": "A|B",
            "relative_capital_strength": 2.0,
            "rank": 1,
            "comparability": {"comparable_to_previous": False},
        }},
        "official_delayed": {"margin": {"as_of_trade_date": "2026-08-07", "financing_balance": 1000.0}},
    }
    change = capital_flow_changes.build_change(before, after)
    assert change["peer_context"]["comparable"] is False
    assert change["peer_context"]["relative_capital_strength"]["delta"] is None
    assert "PEER_UNIVERSE_NONCOMPARABLE" in change["reason_codes"]


def test_new_margin_session_only_changes_when_disclosure_date_advances():
    base = {
        "observed": {"turnover": {"amount_rate_5m": 100, "amount_rate_vs_baseline": 1.0}},
        "derived": {
            "pressure": {"net_bias": "BALANCED"}, "absorption": {"state": "NONE"},
            "price_volume_confirmation": {"state": "NEUTRAL"}, "vwap_acceptance": {"state": "OSCILLATING_AROUND_VWAP"},
        },
        "peer_context": {"primary": {}},
        "official_delayed": {"margin": {"as_of_trade_date": "2026-08-06", "financing_balance": 900}},
    }
    after = json.loads(json.dumps(base))
    after["official_delayed"]["margin"] = {"as_of_trade_date": "2026-08-07", "financing_balance": 1000}
    change = capital_flow_changes.build_change(base, after)
    assert change["margin"]["new_disclosed_session"] is True
    assert change["margin"]["financing_balance"]["delta"] == 100.0
    assert "NEW_MARGIN_DISCLOSURE" in change["reason_codes"]


def main():
    tests = [
        test_observed_turnover_and_directional_structure_are_not_vendor_flow,
        test_pressure_absorption_and_vwap_are_explainable_derived_metrics,
        test_margin_fast_path_is_cache_only_and_truthfully_unverified,
        test_margin_normalization_keeps_official_delayed_semantics_separate_from_provider_trust,
        test_peer_universe_change_blocks_relative_change_comparison,
        test_new_margin_session_only_changes_when_disclosure_date_advances,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"CAPITAL_FLOW_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
