import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import capital_flow_context as capital
import capital_flow_window_bridge
import data_metadata
import data_policy_bridge


data_policy_bridge.install(data_metadata)
capital_flow_window_bridge.install(capital)
CST = timezone(timedelta(hours=8))


def test_regressed_provider_session_cannot_earn_sla_met():
    class Base:
        pass

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

            def old_session(base, code, limit=24):
                return [
                    {"trade_date": "2026-08-06", "financing_balance": 900.0},
                    {"trade_date": "2026-08-05", "financing_balance": 880.0},
                ], "https://example.invalid/provider"

            capital.fetch_margin_history = old_session
            now = datetime(2026, 8, 10, 16, 0, tzinfo=CST)
            margin = capital._margin_context(Base, "002558", now, "FULL", {})
            assert margin["provider_session_regressed"] is True
            assert margin["as_of_trade_date"] == "2026-08-07"
            assert margin["status"] == "CACHED"

            _, _, metadata, _ = capital._build_metadata(
                now.isoformat(timespec="seconds"),
                {"source": "Tencent", "freshness": "LIVE", "lag_seconds": 10},
                margin,
                {"status": "OK"},
                "DEGRADED",
            )
            sla = metadata["freshness_sla"]
            assert sla["status"] == "UNMEASURED"
            assert sla["status"] != "MET"
            assert sla["session_verified"] is False
            assert sla["reason"] == "SESSION_COMPLETENESS_UNVERIFIED"

            persisted = json.loads(cache_path.read_text(encoding="utf-8"))
            assert persisted["records"][0]["trade_date"] == "2026-08-07"
            assert persisted["records"][0]["financing_balance"] == 1000.0
    finally:
        capital.fetch_margin_history = old_fetch
        if old_root is None:
            os.environ.pop("MARKET_HISTORY_DIR", None)
        else:
            os.environ["MARKET_HISTORY_DIR"] = old_root


def main():
    test_regressed_provider_session_cannot_earn_sla_met()
    print("PASS test_regressed_provider_session_cannot_earn_sla_met")
    print("CAPITAL_FLOW_MARGIN_REGRESSION_METADATA_TESTS passed=1")


if __name__ == "__main__":
    main()
