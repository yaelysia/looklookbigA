from datetime import datetime, timedelta, timezone

import live_price_guard


CST = timezone(timedelta(hours=8))


class FakeBase:
    CST = CST

    def __init__(self, payload):
        self._payload = payload
        self.detail_payload = lambda now, code: self._payload

    @staticmethod
    def in_market_window(now):
        return True

    @staticmethod
    def fmt_dt(dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def minutes(now, last_price, minute_offset=0):
    t = now + timedelta(minutes=minute_offset)
    return {
        "source": "Tencent",
        "date": t.strftime("%Y%m%d"),
        "last_time": t.strftime("%H%M"),
        "last_price": last_price,
        "freshness": "LIVE",
    }


def intraday():
    return {
        "price": 99.0,
        "vwap": 10.0,
        "day_high": 12.0,
        "day_low": 8.0,
        "recent_15m_high": 11.0,
        "recent_15m_low": 9.0,
        "recent_30m_high": 12.0,
        "recent_30m_low": 8.0,
    }


def run_case(payload, now):
    base = FakeBase(payload)
    live_price_guard.install(base)
    return base.detail_payload(now, "000001")


def main():
    now = datetime(2026, 8, 7, 14, 30, tzinfo=CST)

    historical_quote = {
        "quote": {"source": "History cache (Tencent qfq)", "latest": 99.0, "freshness": "LIVE"},
        "minutes": minutes(now, 10.2),
        "intraday": intraday(),
        "status": "OK",
        "errors": [],
    }
    result = run_case(historical_quote, now)
    guard = result["intraday"]["current_price_guard"]
    assert guard["status"] == "BLOCKED"
    assert result["quote"]["latest"] is None
    assert result["intraday"]["price"] == 10.2
    assert result["intraday"]["price_source"] == "minute"

    stale_quote_live_minute = {
        "quote": {"source": "Eastmoney", "latest": 11.0, "freshness": "STALE"},
        "minutes": minutes(now, 10.3),
        "intraday": intraday(),
        "status": "OK",
        "errors": [],
    }
    result = run_case(stale_quote_live_minute, now)
    assert result["quote"]["latest"] is None
    assert result["intraday"]["price"] == 10.3
    assert result["intraday"]["current_price_source_class"] == "LIVE_MINUTE"

    all_stale = {
        "quote": {"source": "Eastmoney", "latest": 11.0, "freshness": "STALE"},
        "minutes": minutes(now, 10.4, minute_offset=-10),
        "intraday": intraday(),
        "status": "OK",
        "errors": [],
    }
    result = run_case(all_stale, now)
    assert result["quote"]["latest"] is None
    assert result["minutes"]["last_price"] is None
    assert result["intraday"]["price"] is None
    assert result["intraday"]["current_price_valid"] is False
    assert result["status"] == "PARTIAL"

    print("LIVE_PRICE_GUARD_TESTS_OK cases=3")


if __name__ == "__main__":
    main()
