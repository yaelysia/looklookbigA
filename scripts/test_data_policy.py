from datetime import datetime, timedelta, timezone

import data_metadata
import data_policy
import data_policy_bridge


CST = timezone(timedelta(hours=8))
data_policy_bridge.install(data_metadata)


def test_source_trust_tiers():
    official = data_policy.source_trust("CNINFO", source_type="API", source_tier="OFFICIAL")
    assert official["tier"] == "A"
    assert official["class"] == "OFFICIAL_ORIGINAL"
    assert official["fact_policy"] == "AUTHORITATIVE_FACT"

    primary = data_policy.source_trust("Eastmoney", source_tier="PRIMARY_PROVIDER")
    fallback = data_policy.source_trust("Tencent", source_tier="SECONDARY_PROVIDER")
    assert primary["tier"] == "B"
    assert fallback["tier"] == "B"

    derived = data_policy.source_trust("CNINFO", source_type="DERIVED", source_tier="DERIVED")
    assert derived["tier"] == "INHERITED"
    assert derived["mode"] == "DERIVED"

    cache = data_policy.source_trust("market-data branch", source_type="CACHE", source_tier="CACHE")
    assert cache["tier"] == "INHERITED"
    assert cache["mode"] == "CACHE"

    unknown = data_policy.source_trust("mystery-source")
    assert unknown["tier"] == "UNKNOWN"
    assert unknown["fact_policy"] == "DO_NOT_ASSUME_FACT"
    print("PASS source_trust_tiers")


def test_realtime_quote_sla():
    met = data_policy.evaluate_freshness_sla("REALTIME_QUOTE", freshness="LIVE", lag_seconds=45)
    degraded = data_policy.evaluate_freshness_sla("REALTIME_QUOTE", freshness="LIVE", lag_seconds=120)
    violated = data_policy.evaluate_freshness_sla("REALTIME_QUOTE", freshness="STALE", lag_seconds=181)
    closed = data_policy.evaluate_freshness_sla("REALTIME_QUOTE", freshness="CURRENT_SESSION", lag_seconds=900)
    assert met["status"] == "MET"
    assert degraded["status"] == "DEGRADED"
    assert violated["status"] == "VIOLATED"
    assert closed["status"] == "NOT_APPLICABLE"
    assert met["decision_profiles"]["SHORT_TERM_T"] == 60
    print("PASS realtime_quote_sla")


def test_event_discovery_requires_first_seen():
    published = datetime(2026, 8, 8, 10, 0, tzinfo=CST)
    unmeasured = data_policy.evaluate_freshness_sla(
        "COMPANY_EVENT",
        data_time=published.isoformat(),
        first_seen_at=None,
    )
    met = data_policy.evaluate_freshness_sla(
        "COMPANY_EVENT",
        data_time=published.isoformat(),
        first_seen_at=(published + timedelta(seconds=240)).isoformat(),
    )
    degraded = data_policy.evaluate_freshness_sla(
        "COMPANY_EVENT",
        data_time=published.isoformat(),
        first_seen_at=(published + timedelta(seconds=600)).isoformat(),
    )
    violated = data_policy.evaluate_freshness_sla(
        "COMPANY_EVENT",
        data_time=published.isoformat(),
        first_seen_at=(published + timedelta(seconds=1200)).isoformat(),
    )
    assert unmeasured["status"] == "UNMEASURED"
    assert unmeasured["reason"] == "FIRST_SEEN_AT_OR_PUBLISHED_AT_MISSING"
    assert met["status"] == "MET"
    assert degraded["status"] == "DEGRADED"
    assert violated["status"] == "VIOLATED"
    print("PASS event_discovery_sla")


def test_session_completeness_sla():
    met = data_policy.evaluate_freshness_sla("DAILY_K", freshness="LATEST_COMPLETED_BAR")
    unknown = data_policy.evaluate_freshness_sla("DAILY_K", freshness="UNKNOWN")
    wrong = data_policy.evaluate_freshness_sla("DAILY_K", freshness="HISTORICAL")
    assert met["status"] == "MET"
    assert unknown["status"] == "UNMEASURED"
    assert wrong["status"] == "VIOLATED"
    print("PASS session_completeness_sla")


def test_future_data_classes_are_predeclared():
    required = {
        "NEWS",
        "RESEARCH_REPORT",
        "DRAGON_TIGER_LIST",
        "REGULATORY_EVENT",
        "INTRADAY_FUND_FLOW",
        "DAILY_FINANCING",
        "MACRO_RELEASE",
        "INDUSTRY_EVENT",
    }
    assert required.issubset(data_policy.FRESHNESS_POLICIES)
    assert data_policy.FRESHNESS_POLICIES["NEWS"]["measurement"] == "DISCOVERY_LAG"
    assert data_policy.FRESHNESS_POLICIES["INTRADAY_FUND_FLOW"]["measurement"] == "DATA_LAG"
    print("PASS future_data_classes_predeclared")


def test_metadata_bridge_attaches_policy_contract():
    quote = data_metadata._metadata(
        "Eastmoney",
        "2026-08-08T10:00:45+08:00",
        data_time="2026-08-08T10:00:00+08:00",
        lag_seconds=45,
        freshness="LIVE",
        freshness_policy="REALTIME_QUOTE",
        quality="PASS",
        source_type="API",
        source_tier="PRIMARY_PROVIDER",
    )
    assert quote["trust"]["tier"] == "B"
    assert quote["freshness_sla"]["data_class"] == "REALTIME_QUOTE"
    assert quote["freshness_sla"]["status"] == "MET"

    event = data_metadata._metadata(
        "CNINFO",
        "2026-08-08T10:05:00+08:00",
        data_time="2026-08-08T10:00:00+08:00",
        freshness="CURRENT",
        freshness_policy="OFFICIAL_DISCLOSURE",
        quality="PASS",
        source_type="API",
        source_tier="OFFICIAL",
    )
    assert event["trust"]["tier"] == "A"
    assert event["freshness_sla"]["data_class"] == "COMPANY_EVENT"
    assert event["freshness_sla"]["status"] == "UNMEASURED"
    print("PASS metadata_policy_contract")


def test_snapshot_policy_manifest_and_summary():
    snapshot = {
        "runner_time_utc": "2026-08-08T02:00:00Z",
        "detail_stocks": {},
        "light_stocks": {},
        "indices": {},
        "groups": {},
    }
    data_metadata.decorate_snapshot(snapshot)
    assert snapshot["schema_version"] >= 13
    assert snapshot["features"]["data_policy"] == "v1"
    assert snapshot["data_policy"]["source_trust_model"]["version"] == data_policy.SOURCE_TRUST_MODEL_VERSION
    assert snapshot["data_policy"]["freshness_sla"]["version"] == data_policy.FRESHNESS_SLA_VERSION
    assert snapshot["data_policy"]["current_capabilities"]["continuous_watcher"] == "NOT_IMPLEMENTED"
    assert snapshot["data_quality"]["policy_versions"]["freshness_sla"] == data_policy.FRESHNESS_SLA_VERSION
    print("PASS snapshot_policy_manifest")


def main():
    tests = [
        test_source_trust_tiers,
        test_realtime_quote_sla,
        test_event_discovery_requires_first_seen,
        test_session_completeness_sla,
        test_future_data_classes_are_predeclared,
        test_metadata_bridge_attaches_policy_contract,
        test_snapshot_policy_manifest_and_summary,
    ]
    for test in tests:
        test()
    print(f"DATA_POLICY_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
