from datetime import datetime, timezone, timedelta

import quote_resilience as qr


CST = timezone(timedelta(hours=8))


class FakeBase:
    @staticmethod
    def in_market_window(now):
        return True


BASE = FakeBase()
NOW = datetime(2026, 8, 7, 14, 30, tzinfo=CST)


def quote(source, latest, freshness="LIVE", lag=1):
    return {
        "source": source,
        "latest": latest,
        "freshness": freshness,
        "lag_seconds": lag,
        "market_time_cst": "2026-08-07 14:30:00",
    }


def test_primary_consistent():
    selected = qr._select_quote(BASE, NOW, quote("Eastmoney", 29.20), quote("Tencent", 29.20, lag=2))
    assert selected["source"] == "Eastmoney"
    assert selected["resilience"]["fallback_used"] is False
    assert selected["resilience"]["consensus"]["status"] == "CONSISTENT"


def test_primary_failure_fallback():
    selected = qr._select_quote(
        BASE,
        NOW,
        None,
        quote("Tencent", 29.20),
        primary_error="HTTP 502",
    )
    assert selected["source"] == "Tencent"
    assert selected["resilience"]["fallback_used"] is True
    assert selected["resilience"]["selection_reason"] == "PRIMARY_UNUSABLE_FALLBACK_USABLE"


def test_stale_primary_live_fallback():
    selected = qr._select_quote(
        BASE,
        NOW,
        quote("Eastmoney", 29.10, freshness="STALE", lag=500),
        quote("Tencent", 29.20, freshness="LIVE", lag=2),
    )
    assert selected["source"] == "Tencent"
    assert selected["latest"] == 29.20


def test_divergent_choose_newer():
    selected = qr._select_quote(
        BASE,
        NOW,
        quote("Eastmoney", 29.00, freshness="LIVE", lag=20),
        quote("Tencent", 29.30, freshness="LIVE", lag=1),
    )
    assert selected["resilience"]["consensus"]["status"] == "DIVERGENT"
    assert selected["source"] == "Tencent"
    assert "DIVERGENT_FALLBACK_NEWER" in selected["resilience"]["selection_reason"]


def test_both_stale_are_diagnostic_only():
    selected = qr._select_quote(
        BASE,
        NOW,
        quote("Eastmoney", 29.10, freshness="STALE", lag=500),
        quote("Tencent", 29.11, freshness="STALE", lag=600),
    )
    assert qr._is_usable(BASE, NOW, selected) is False
    assert selected["resilience"]["selection_reason"] == "NO_USABLE_SOURCE_RETURNING_STALE_FOR_DIAGNOSTICS"


def test_no_source_raises():
    try:
        qr._select_quote(BASE, NOW, None, None, "eastmoney down", "tencent down")
    except RuntimeError as exc:
        assert "no quote source available" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def main():
    tests = [
        test_primary_consistent,
        test_primary_failure_fallback,
        test_stale_primary_live_fallback,
        test_divergent_choose_newer,
        test_both_stale_are_diagnostic_only,
        test_no_source_raises,
    ]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"QUOTE_RESILIENCE_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
