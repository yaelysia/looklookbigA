import json
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


CST = ZoneInfo("Asia/Shanghai")
DEFAULT_CALENDAR_PATH = Path("config/a_share_trading_calendar.json")


class CalendarConfigError(ValueError):
    pass


def _parse_date(value, field="date"):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise CalendarConfigError(f"{field} datetime must be timezone-aware")
        return value.astimezone(CST).date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise CalendarConfigError(f"{field} must be YYYY-MM-DD") from exc


def _parse_time(value, field):
    try:
        parsed = time.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise CalendarConfigError(f"{field} must be HH:MM[:SS]") from exc
    if parsed.tzinfo is not None:
        raise CalendarConfigError(f"{field} must be a local market time")
    return parsed


def _aware_cst(value):
    if not isinstance(value, datetime):
        raise CalendarConfigError("now must be a datetime")
    if value.tzinfo is None:
        raise CalendarConfigError("now must be timezone-aware")
    return value.astimezone(CST)


def _expand_range(start, end):
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


@lru_cache(maxsize=4)
def _load_calendar_file(path_text):
    path = Path(path_text)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CalendarConfigError(f"cannot load trading calendar: {path}") from exc

    required = (
        "calendar_version",
        "market",
        "timezone",
        "valid_from",
        "valid_through",
        "regular_weekdays",
        "source",
        "session",
    )
    missing = [field for field in required if field not in raw]
    if missing:
        raise CalendarConfigError("calendar missing fields: " + ",".join(missing))
    if raw["timezone"] != "Asia/Shanghai":
        raise CalendarConfigError("calendar timezone must be Asia/Shanghai")

    valid_from = _parse_date(raw["valid_from"], "valid_from")
    valid_through = _parse_date(raw["valid_through"], "valid_through")
    if valid_from > valid_through:
        raise CalendarConfigError("calendar valid_from is after valid_through")

    weekdays = tuple(raw.get("regular_weekdays") or [])
    if not weekdays or any(not isinstance(day, int) or day < 0 or day > 6 for day in weekdays):
        raise CalendarConfigError("regular_weekdays must contain weekday integers")
    if len(set(weekdays)) != len(weekdays):
        raise CalendarConfigError("regular_weekdays contains duplicates")

    closed_dates = {}
    for item in raw.get("closed_ranges") or []:
        if not isinstance(item, dict) or not item.get("reason"):
            raise CalendarConfigError("closed range must be an object with reason")
        start = _parse_date(item.get("start"), "closed_range.start")
        end = _parse_date(item.get("end"), "closed_range.end")
        if start > end or start < valid_from or end > valid_through:
            raise CalendarConfigError("closed range is invalid or outside calendar coverage")
        for closed in _expand_range(start, end):
            previous = closed_dates.get(closed)
            if previous and previous != item["reason"]:
                raise CalendarConfigError(f"conflicting closure reason for {closed}")
            closed_dates[closed] = str(item["reason"])

    session = dict(raw["session"])
    time_fields = (
        "preopen_at",
        "morning_start",
        "morning_end",
        "afternoon_start",
        "afternoon_end",
        "daily_bar_final_after",
    )
    parsed_times = {field: _parse_time(session.get(field), f"session.{field}") for field in time_fields}
    if not (
        parsed_times["preopen_at"] <= parsed_times["morning_start"]
        < parsed_times["morning_end"]
        < parsed_times["afternoon_start"]
        < parsed_times["afternoon_end"]
        <= parsed_times["daily_bar_final_after"]
    ):
        raise CalendarConfigError("session times are not strictly ordered")

    def parse_windows(name):
        windows = []
        for idx, pair in enumerate(session.get(name) or []):
            if not isinstance(pair, list) or len(pair) != 2:
                raise CalendarConfigError(f"session.{name}[{idx}] must be [start,end]")
            start = _parse_time(pair[0], f"session.{name}[{idx}].start")
            end = _parse_time(pair[1], f"session.{name}[{idx}].end")
            if start > end:
                raise CalendarConfigError(f"session.{name}[{idx}] start is after end")
            windows.append((start, end))
        if not windows:
            raise CalendarConfigError(f"session.{name} cannot be empty")
        return tuple(windows)

    grace = session.get("minute_finality_grace_seconds")
    if not isinstance(grace, int) or grace < 0 or grace > 120:
        raise CalendarConfigError("minute_finality_grace_seconds must be between 0 and 120")

    normalized = dict(raw)
    normalized.update(
        {
            "valid_from_date": valid_from,
            "valid_through_date": valid_through,
            "regular_weekday_set": frozenset(weekdays),
            "closed_dates": closed_dates,
            "session_times": parsed_times,
            "acquisition_windows_parsed": parse_windows("acquisition_windows"),
            "minute_label_ranges_parsed": parse_windows("minute_label_ranges"),
        }
    )
    return normalized


def load_calendar(path=None):
    return _load_calendar_file(str(Path(path or DEFAULT_CALENDAR_PATH).resolve()))


def trading_day_verification(value, calendar=None):
    calendar = calendar or load_calendar()
    day = _parse_date(value)
    if day < calendar["valid_from_date"] or day > calendar["valid_through_date"]:
        return {
            "date": day.isoformat(),
            "is_trading_day": None,
            "verification_status": "UNVERIFIED",
            "reason": "OUTSIDE_CALENDAR_COVERAGE",
        }
    if day.weekday() not in calendar["regular_weekday_set"]:
        return {
            "date": day.isoformat(),
            "is_trading_day": False,
            "verification_status": "VERIFIED",
            "reason": "WEEKEND",
        }
    closure = calendar["closed_dates"].get(day)
    return {
        "date": day.isoformat(),
        "is_trading_day": closure is None,
        "verification_status": "VERIFIED",
        "reason": closure or "REGULAR_TRADING_DAY",
    }


def is_trading_day(value, calendar=None):
    return trading_day_verification(value, calendar).get("is_trading_day")


def previous_trading_day(value, calendar=None):
    calendar = calendar or load_calendar()
    cursor = _parse_date(value) - timedelta(days=1)
    while cursor >= calendar["valid_from_date"]:
        status = trading_day_verification(cursor, calendar)
        if status["is_trading_day"] is True:
            return cursor
        cursor -= timedelta(days=1)
    return None


def next_trading_day(value, calendar=None):
    calendar = calendar or load_calendar()
    cursor = _parse_date(value) + timedelta(days=1)
    while cursor <= calendar["valid_through_date"]:
        status = trading_day_verification(cursor, calendar)
        if status["is_trading_day"] is True:
            return cursor
        cursor += timedelta(days=1)
    return None


def current_session_date(now, calendar=None):
    now = _aware_cst(now)
    return now.date() if is_trading_day(now.date(), calendar) is True else None


def session_state(now, calendar=None):
    calendar = calendar or load_calendar()
    now = _aware_cst(now)
    day = trading_day_verification(now.date(), calendar)
    if day["verification_status"] != "VERIFIED":
        return "UNVERIFIED"
    if day["is_trading_day"] is not True:
        return "NON_TRADING_DAY"
    current = now.time().replace(tzinfo=None)
    session = calendar["session_times"]
    if current < session["morning_start"]:
        return "TRADING_DAY_PREOPEN"
    if current < (datetime.combine(now.date(), session["morning_end"]) + timedelta(minutes=1)).time():
        return "TRADING_DAY_MORNING"
    if current < session["afternoon_start"]:
        return "TRADING_DAY_LUNCH_BREAK"
    if current < (datetime.combine(now.date(), session["afternoon_end"]) + timedelta(minutes=1)).time():
        return "TRADING_DAY_AFTERNOON"
    return "TRADING_DAY_CLOSED"


def in_market_window(now, calendar=None):
    calendar = calendar or load_calendar()
    now = _aware_cst(now)
    if is_trading_day(now.date(), calendar) is not True:
        return False
    current = now.time().replace(tzinfo=None)
    return any(start <= current <= end for start, end in calendar["acquisition_windows_parsed"])


def previous_completed_session(now, calendar=None):
    calendar = calendar or load_calendar()
    now = _aware_cst(now)
    verification = trading_day_verification(now.date(), calendar)
    if verification["verification_status"] != "VERIFIED":
        return None
    if (
        verification["is_trading_day"] is True
        and now.time().replace(tzinfo=None) >= calendar["session_times"]["daily_bar_final_after"]
    ):
        return now.date()
    return previous_trading_day(now.date(), calendar)


def _minute_labels(calendar):
    labels = []
    anchor = date(2000, 1, 1)
    for start, end in calendar["minute_label_ranges_parsed"]:
        cursor = datetime.combine(anchor, start)
        limit = datetime.combine(anchor, end)
        while cursor <= limit:
            labels.append(cursor.strftime("%H%M"))
            cursor += timedelta(minutes=1)
    return tuple(labels)


def expected_minute_times(now, session_date=None, calendar=None):
    calendar = calendar or load_calendar()
    now = _aware_cst(now)
    target = _parse_date(session_date or now.date(), "session_date")
    verification = trading_day_verification(target, calendar)
    base = {
        "session_date": target.isoformat(),
        "verification_status": verification["verification_status"],
        "expected_times": [],
        "expected_count": 0,
        "forming_minute": None,
    }
    if verification["verification_status"] != "VERIFIED":
        return {**base, "status": "UNVERIFIED", "reason": verification["reason"]}
    if verification["is_trading_day"] is not True:
        return {**base, "status": "NOT_APPLICABLE", "reason": verification["reason"]}
    if target > now.date():
        return {**base, "status": "FUTURE_SESSION", "reason": "SESSION_NOT_STARTED"}

    labels = _minute_labels(calendar)
    if target < now.date():
        return {
            **base,
            "status": "COMPLETE_SESSION",
            "expected_times": list(labels),
            "expected_count": len(labels),
        }

    grace = timedelta(seconds=calendar["session"]["minute_finality_grace_seconds"])
    endpoints = {end.strftime("%H%M") for _, end in calendar["minute_label_ranges_parsed"]}
    completed = []
    forming = None
    for label in labels:
        label_time = datetime.strptime(label, "%H%M").time()
        label_dt = datetime.combine(target, label_time, tzinfo=CST)
        final_at = label_dt + grace if label in endpoints else label_dt + timedelta(minutes=1) + grace
        if now >= final_at:
            completed.append(label)
        elif label_dt <= now < final_at:
            forming = label
            break
        elif label_dt > now:
            break
    return {
        **base,
        "status": "IN_PROGRESS" if session_state(now, calendar) not in {"TRADING_DAY_CLOSED"} else "COMPLETE_SESSION",
        "expected_times": completed,
        "expected_count": len(completed),
        "forming_minute": forming,
    }


def snapshot_context(now, calendar=None):
    calendar = calendar or load_calendar()
    now = _aware_cst(now)
    verification = trading_day_verification(now.date(), calendar)
    previous = previous_completed_session(now, calendar)
    next_day = next_trading_day(now.date(), calendar)
    minutes = expected_minute_times(now, calendar=calendar)
    quality = "PASS" if verification["verification_status"] == "VERIFIED" else "DEGRADED"
    return {
        "market": calendar["market"],
        "timezone": calendar["timezone"],
        "calendar_version": calendar["calendar_version"],
        "calendar_as_of": calendar.get("as_of"),
        "session_date": current_session_date(now, calendar).isoformat()
        if current_session_date(now, calendar)
        else None,
        "session_state": session_state(now, calendar),
        "is_trading_day": verification["is_trading_day"],
        "previous_completed_session": previous.isoformat() if previous else None,
        "next_trading_day": next_day.isoformat() if next_day else None,
        "verification_status": verification["verification_status"],
        "quality": quality,
        "reason_codes": [] if quality == "PASS" else [verification["reason"]],
        "expected_minutes": {
            "status": minutes["status"],
            "completed_count": minutes["expected_count"],
            "last_completed_minute": minutes["expected_times"][-1] if minutes["expected_times"] else None,
            "forming_minute": minutes["forming_minute"],
        },
        "provenance": {
            "authority": calendar["source"].get("authority"),
            "source_trust": calendar["source"].get("trust"),
            "method": calendar["source"].get("method"),
            "valid_from": calendar["valid_from"],
            "valid_through": calendar["valid_through"],
            "documents": calendar["source"].get("documents") or [],
        },
    }


def _snapshot_time(data):
    value = data.get("runner_time_cst")
    if not value:
        return datetime.now(CST)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalendarConfigError("snapshot runner_time_cst is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["market_calendar"] = snapshot_context(_snapshot_time(data))
    data["schema_version"] = max(int(data.get("schema_version") or 0), 18)
    data.setdefault("features", {})["market_calendar"] = "v1"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    context = data["market_calendar"]
    print(
        "MARKET_CALENDAR "
        f"state={context['session_state']} previous={context['previous_completed_session']} "
        f"verification={context['verification_status']} quality={context['quality']}",
        flush=True,
    )
