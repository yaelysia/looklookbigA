import json
from datetime import datetime
from pathlib import Path


FORBIDDEN_CURRENT_SOURCE_TOKENS = ("history", "cache", "snapshot", "archive", "daily_k")


def _as_float(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(current, reference):
    current = _as_float(current)
    reference = _as_float(reference)
    if current is None or reference in (None, 0):
        return None
    return (current / reference - 1.0) * 100.0


def _round(value, digits=4):
    return None if value is None else round(float(value), digits)


def _forbidden_source(source):
    text = str(source or "").lower()
    return any(token in text for token in FORBIDDEN_CURRENT_SOURCE_TOKENS)


def _minute_datetime(base, minutes):
    date = str((minutes or {}).get("date") or "")
    raw_time = str((minutes or {}).get("last_time") or "")
    digits = "".join(ch for ch in raw_time if ch.isdigit())
    if len(date) != 8 or not date.isdigit() or len(digits) < 4:
        return None
    try:
        return datetime.strptime(date + digits[:4], "%Y%m%d%H%M").replace(tzinfo=base.CST)
    except ValueError:
        return None


def _refresh_minute_freshness(base, now, minutes):
    if not isinstance(minutes, dict):
        return "UNKNOWN", None
    mdt = _minute_datetime(base, minutes)
    if mdt is None:
        minutes["freshness"] = "UNKNOWN"
        minutes["market_time_cst"] = None
        minutes["lag_seconds"] = None
        return "UNKNOWN", None

    lag = max(0, int((now - mdt).total_seconds()))
    same_day = mdt.date() == now.date()
    if base.in_market_window(now):
        state = "LIVE" if same_day and lag <= 180 else "STALE"
    else:
        state = "CURRENT_SESSION" if same_day else "LAST_SESSION"

    minutes["freshness"] = state
    minutes["market_time_cst"] = base.fmt_dt(mdt)
    minutes["lag_seconds"] = lag
    return state, lag


def _recompute_intraday_price_fields(intraday, price):
    intraday["price"] = price
    vwap = _as_float(intraday.get("vwap"))
    day_high = _as_float(intraday.get("day_high"))
    day_low = _as_float(intraday.get("day_low"))
    recent15_high = _as_float(intraday.get("recent_15m_high"))
    recent15_low = _as_float(intraday.get("recent_15m_low"))
    recent30_high = _as_float(intraday.get("recent_30m_high"))
    recent30_low = _as_float(intraday.get("recent_30m_low"))

    intraday["price_vs_vwap_percent"] = _round(_pct(price, vwap), 4)
    intraday["above_vwap"] = None if price is None or vwap is None else price >= vwap
    intraday["from_day_high_percent"] = _round(_pct(price, day_high), 4)
    intraday["from_day_low_percent"] = _round(_pct(price, day_low), 4)
    intraday["distance_to_recent_15m_high_percent"] = _round(_pct(price, recent15_high), 4)
    intraday["from_recent_15m_low_percent"] = _round(_pct(price, recent15_low), 4)
    intraday["distance_to_recent_30m_high_percent"] = _round(_pct(price, recent30_high), 4)
    intraday["from_recent_30m_low_percent"] = _round(_pct(price, recent30_low), 4)

    if price is not None and day_high is not None and day_low is not None and day_high > day_low:
        intraday["day_range_position_percent"] = _round((price - day_low) / (day_high - day_low) * 100.0, 2)
    else:
        intraday["day_range_position_percent"] = None


def install(base):
    original_detail_payload = base.detail_payload

    def guarded_detail_payload(now, code):
        result = original_detail_payload(now, code)
        quote = result.get("quote") or {}
        minutes = result.get("minutes") or {}
        intraday = result.get("intraday") or {}
        result["intraday"] = intraday

        minute_state, minute_lag = _refresh_minute_freshness(base, now, minutes)
        quote_source = quote.get("source")
        minute_source = minutes.get("source")
        hard_violations = []

        if quote and _forbidden_source(quote_source):
            hard_violations.append(f"forbidden quote source: {quote_source}")
        if minutes and _forbidden_source(minute_source):
            hard_violations.append(f"forbidden minute source: {minute_source}")

        in_market = base.in_market_window(now)
        quote_price = _as_float(quote.get("latest"))
        minute_price = _as_float(minutes.get("last_price"))
        quote_state = quote.get("freshness")

        quote_valid = (
            quote_price is not None
            and not _forbidden_source(quote_source)
            and (not in_market or quote_state == "LIVE")
        )
        minute_valid = (
            minute_price is not None
            and not _forbidden_source(minute_source)
            and (not in_market or minute_state == "LIVE")
        )

        if quote_valid:
            current_price = quote_price
            price_source = "quote"
            source_class = "LIVE_QUOTE" if in_market else "SESSION_QUOTE"
            provider = quote_source
        elif minute_valid:
            current_price = minute_price
            price_source = "minute"
            source_class = "LIVE_MINUTE" if in_market else "SESSION_MINUTE"
            provider = minute_source
        else:
            current_price = None
            price_source = "none"
            source_class = "UNAVAILABLE"
            provider = None

        # Sanitize downstream fallbacks. During market hours, an invalid/stale
        # provider must not remain available through quote.latest or
        # minutes.last_price, otherwise daily-context code could accidentally
        # consume it after the guard selected a safer source.
        if in_market and not quote_valid and quote:
            quote["latest"] = None
            quote["current_price_blocked_by_guard"] = True
        if in_market and not minute_valid and minutes:
            minutes["last_price"] = None
            minutes["current_price_blocked_by_guard"] = True

        _recompute_intraday_price_fields(intraday, current_price)
        intraday["price_source"] = price_source
        intraday["current_price_valid"] = current_price is not None
        intraday["current_price_source_class"] = source_class
        intraday["current_price_provider"] = provider
        intraday["current_price_guard"] = {
            "status": "BLOCKED" if hard_violations else ("OK" if current_price is not None else "WARNING"),
            "market_window": in_market,
            "quote_freshness": quote_state,
            "minute_freshness": minute_state,
            "minute_lag_seconds": minute_lag,
            "hard_violations": hard_violations,
        }

        if hard_violations:
            result.setdefault("errors", []).append("live_price_guard: " + "; ".join(hard_violations))
            result["status"] = "PARTIAL"
        elif in_market and current_price is None:
            result.setdefault("errors", []).append(
                f"live_price_guard: no LIVE current price (quote={quote_state}, minute={minute_state})"
            )
            result["status"] = "PARTIAL"
        elif in_market and not quote_valid and minute_valid and result.get("status") == "OK":
            result["status"] = "PARTIAL"
            result.setdefault("errors", []).append(
                f"live_price_guard: quote not LIVE ({quote_state}); using LIVE minute price"
            )

        return result

    base.detail_payload = guarded_detail_payload


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    market_window = bool(data.get("market_window"))
    hard = []
    warnings = []

    for code, item in (data.get("detail_stocks") or {}).items():
        quote = item.get("quote") or {}
        intraday = item.get("intraday") or {}
        source = quote.get("source")
        if quote and _forbidden_source(source):
            hard.append(f"{code}: historical/cache source reached quote.latest ({source})")
        guard = intraday.get("current_price_guard") or {}
        hard.extend(f"{code}: {msg}" for msg in guard.get("hard_violations") or [])
        if market_window and not intraday.get("current_price_valid"):
            warnings.append(f"{code}: no valid LIVE current price")
        if market_window and quote and quote.get("freshness") != "LIVE":
            warnings.append(f"{code}: quote freshness={quote.get('freshness')}")

    for code, item in (data.get("light_stocks") or {}).items():
        quote = (item or {}).get("quote") or {}
        source = quote.get("source")
        if quote and _forbidden_source(source):
            hard.append(f"{code}: historical/cache source reached light quote ({source})")
        if market_window and quote and quote.get("freshness") != "LIVE":
            warnings.append(f"{code}: light quote freshness={quote.get('freshness')}")

    data["schema_version"] = max(int(data.get("schema_version") or 0), 7)
    data.setdefault("features", {})["live_price_guard"] = "v1"
    data["live_price_guard"] = {
        "status": "BLOCKED" if hard else ("WARNING" if warnings else "OK"),
        "hard_violations": hard,
        "warnings": warnings,
        "policy": {
            "historical_sources_allowed_for_current_price": False,
            "market_live_price_sources": ["live quote", "live same-day minute"],
            "stale_quote_may_fallback_to_live_minute": True,
        },
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"LIVE_PRICE_GUARD status={data['live_price_guard']['status']} hard={len(hard)} warnings={len(warnings)}",
        flush=True,
    )
    print("SNAPSHOT_SCHEMA_UPGRADED schema_version=7 feature=live_price_guard:v1", flush=True)
    if hard:
        raise RuntimeError("live price guard blocked snapshot: " + "; ".join(hard))
