import json
import tempfile
from datetime import datetime
from pathlib import Path

import daily_k_context
import market_calendar


def cst(value):
    return datetime.fromisoformat(value).replace(tzinfo=market_calendar.CST)


def test_official_closures_and_coverage_fail_closed():
    assert market_calendar.is_trading_day("2026-02-17") is False
    assert market_calendar.is_trading_day("2026-02-24") is True
    assert market_calendar.is_trading_day("2026-02-28") is False
    outside = market_calendar.trading_day_verification("2027-01-04")
    assert outside["is_trading_day"] is None
    assert outside["verification_status"] == "UNVERIFIED"


def test_session_states_and_previous_completed_session():
    trading_day = "2026-02-24"
    assert market_calendar.session_state(cst(trading_day + "T09:20:00")) == "TRADING_DAY_PREOPEN"
    assert market_calendar.session_state(cst(trading_day + "T10:00:00")) == "TRADING_DAY_MORNING"
    assert market_calendar.session_state(cst(trading_day + "T12:00:00")) == "TRADING_DAY_LUNCH_BREAK"
    assert market_calendar.session_state(cst(trading_day + "T14:00:00")) == "TRADING_DAY_AFTERNOON"
    assert market_calendar.session_state(cst(trading_day + "T15:06:00")) == "TRADING_DAY_CLOSED"
    assert market_calendar.previous_completed_session(cst(trading_day + "T10:00:00")).isoformat() == "2026-02-13"
    assert market_calendar.previous_completed_session(cst(trading_day + "T15:06:00")).isoformat() == trading_day
    assert market_calendar.session_state(cst("2026-02-17T10:00:00")) == "NON_TRADING_DAY"
    assert market_calendar.in_market_window(cst("2026-02-17T10:00:00")) is False


def test_expected_minutes_exclude_lunch_and_forming_bar():
    morning = market_calendar.expected_minute_times(cst("2026-02-24T12:00:00"))
    assert morning["expected_count"] == 121
    assert morning["expected_times"][0] == "0930"
    assert morning["expected_times"][-1] == "1130"
    assert all(not value.startswith("12") for value in morning["expected_times"])

    forming = market_calendar.expected_minute_times(cst("2026-02-24T10:05:30"))
    assert forming["forming_minute"] == "1005"
    assert forming["expected_times"][-1] == "1004"


def test_daily_k_stale_bar_is_not_latest_completed():
    bars = [
        {"date": "2026-02-12", "close": 10},
        {"date": "2026-02-24", "close": 11},
    ]
    completed, current, validation = daily_k_context._split_completed_bars(
        cst("2026-02-24T10:00:00"), bars
    )
    assert [bar["date"] for bar in completed] == ["2026-02-12"]
    assert current["date"] == "2026-02-24"
    assert validation["expected_previous_completed_session"] == "2026-02-13"
    assert validation["status"] == "STALE_COMPLETED_BAR"


def test_snapshot_context_is_auditable():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(
            json.dumps({"schema_version": 17, "runner_time_cst": "2026-02-24 10:00:00"}),
            encoding="utf-8",
        )
        market_calendar.finalize_snapshot(path)
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        context = snapshot["market_calendar"]
        assert snapshot["schema_version"] == 18
        assert context["quality"] == "PASS"
        assert context["previous_completed_session"] == "2026-02-13"
        assert context["provenance"]["documents"]


def main():
    tests = [
        test_official_closures_and_coverage_fail_closed,
        test_session_states_and_previous_completed_session,
        test_expected_minutes_exclude_lunch_and_forming_bar,
        test_daily_k_stale_bar_is_not_latest_completed,
        test_snapshot_context_is_auditable,
    ]
    for test in tests:
        test()
    print(f"MARKET_CALENDAR_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
