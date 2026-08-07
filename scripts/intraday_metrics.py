import json
import statistics
from pathlib import Path


def _as_float(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value, digits=4):
    return None if value is None else round(float(value), digits)


def _pct_change(current, reference):
    current = _as_float(current)
    reference = _as_float(reference)
    if current is None or reference in (None, 0):
        return None
    return (current / reference - 1.0) * 100.0


def _mean(values):
    values = [float(x) for x in values if x is not None]
    return statistics.fmean(values) if values else None


def _median(values):
    values = [float(x) for x in values if x is not None]
    return statistics.median(values) if values else None


def _window_prices(mins, count):
    if not mins:
        return []
    return [float(x["price"]) for x in mins[-min(count, len(mins)) :]]


def _trend(mins, minutes):
    if not mins:
        return None
    current = mins[-1]["price"]
    reference_index = -(minutes + 1)
    reference = mins[reference_index]["price"] if len(mins) >= minutes + 1 else mins[0]["price"]
    return _pct_change(current, reference)


def _range_metrics(mins, count):
    prices = _window_prices(mins, count)
    if not prices:
        return None, None
    return max(prices), min(prices)


def _ratio(value, baseline):
    value = _as_float(value)
    baseline = _as_float(baseline)
    if value is None or baseline in (None, 0):
        return None
    return value / baseline


def _activity_metrics(mins):
    # Tencent's newest minute can still be accumulating. Use the prior minute
    # as the activity reference when possible so volume spikes are not biased
    # downward by an incomplete bar.
    if len(mins) < 3:
        return {
            "reference_time": None,
            "volume_spike_ratio_1m": None,
            "amount_spike_ratio_1m": None,
            "volume_strength_ratio_5m": None,
            "amount_strength_ratio_5m": None,
            "baseline_minutes": 0,
        }

    ref_index = len(mins) - 2
    ref = mins[ref_index]
    prior_start = max(0, ref_index - 20)
    prior = mins[prior_start:ref_index]

    baseline_volumes = [x.get("delta_volume") for x in prior if (x.get("delta_volume") or 0) > 0]
    baseline_amounts = [x.get("delta_amount") for x in prior if (x.get("delta_amount") or 0) > 0]
    volume_median = _median(baseline_volumes)
    amount_median = _median(baseline_amounts)

    last5_start = max(0, ref_index - 4)
    last5 = mins[last5_start : ref_index + 1]
    preceding_start = max(0, last5_start - 20)
    preceding = mins[preceding_start:last5_start]

    last5_volume_mean = _mean([x.get("delta_volume") for x in last5 if (x.get("delta_volume") or 0) > 0])
    last5_amount_mean = _mean([x.get("delta_amount") for x in last5 if (x.get("delta_amount") or 0) > 0])
    prior_volume_mean = _mean([x.get("delta_volume") for x in preceding if (x.get("delta_volume") or 0) > 0])
    prior_amount_mean = _mean([x.get("delta_amount") for x in preceding if (x.get("delta_amount") or 0) > 0])

    return {
        "reference_time": ref.get("time"),
        "volume_spike_ratio_1m": _round(_ratio(ref.get("delta_volume"), volume_median), 3),
        "amount_spike_ratio_1m": _round(_ratio(ref.get("delta_amount"), amount_median), 3),
        "volume_strength_ratio_5m": _round(_ratio(last5_volume_mean, prior_volume_mean), 3),
        "amount_strength_ratio_5m": _round(_ratio(last5_amount_mean, prior_amount_mean), 3),
        "baseline_minutes": len(prior),
    }


def _append_unique_pivot(items, point):
    if items and items[-1]["time"] == point["time"]:
        return
    items.append(point)


def _swing_points(mins, lookback=60, radius=2):
    data = mins[-min(lookback, len(mins)) :]
    highs = []
    lows = []
    if len(data) < radius * 2 + 1:
        return highs, lows

    for i in range(radius, len(data) - radius):
        window = data[i - radius : i + radius + 1]
        prices = [float(x["price"]) for x in window]
        current = float(data[i]["price"])

        max_price = max(prices)
        min_price = min(prices)
        if current == max_price and prices.index(max_price) == radius and min_price < max_price:
            _append_unique_pivot(highs, {"time": data[i]["time"], "price": current})
        if current == min_price and prices.index(min_price) == radius and min_price < max_price:
            _append_unique_pivot(lows, {"time": data[i]["time"], "price": current})

    return highs, lows


def _level_direction(previous, current, tolerance_percent=0.08):
    delta = _pct_change(current, previous)
    if delta is None:
        return "UNKNOWN"
    if delta > tolerance_percent:
        return "HIGHER"
    if delta < -tolerance_percent:
        return "LOWER"
    return "FLAT"


def _structure_metrics(mins):
    highs, lows = _swing_points(mins, lookback=60, radius=2)
    high_direction = "UNKNOWN"
    low_direction = "UNKNOWN"

    if len(highs) >= 2:
        high_direction = _level_direction(highs[-2]["price"], highs[-1]["price"])
    if len(lows) >= 2:
        low_direction = _level_direction(lows[-2]["price"], lows[-1]["price"])

    if high_direction == "HIGHER" and low_direction == "HIGHER":
        structure = "HIGHER_HIGH_HIGHER_LOW"
        bias = "UPTREND"
    elif high_direction == "LOWER" and low_direction == "LOWER":
        structure = "LOWER_HIGH_LOWER_LOW"
        bias = "DOWNTREND"
    elif high_direction == "LOWER" and low_direction == "HIGHER":
        structure = "LOWER_HIGH_HIGHER_LOW"
        bias = "COMPRESSION"
    elif high_direction == "HIGHER" and low_direction == "LOWER":
        structure = "HIGHER_HIGH_LOWER_LOW"
        bias = "EXPANDING_RANGE"
    elif high_direction != "UNKNOWN" and low_direction != "UNKNOWN":
        structure = "RANGE_OR_FLAT"
        bias = "RANGE"
    else:
        structure = "INSUFFICIENT_SWINGS"
        bias = "UNCONFIRMED"

    return {
        "structure_window_minutes": min(60, len(mins)),
        "structure": structure,
        "bias": bias,
        "swing_high_direction": high_direction,
        "swing_low_direction": low_direction,
        "last_swing_highs": highs[-2:],
        "last_swing_lows": lows[-2:],
    }


def build_intraday_metrics(quote, mins):
    if not mins:
        return {
            "status": "NO_MINUTE_DATA",
            "minute_count": 0,
        }

    quote = quote or {}
    minute_price = _as_float(mins[-1].get("price"))
    quote_price = _as_float(quote.get("latest"))
    price = quote_price if quote_price is not None else minute_price

    vwap = _as_float(quote.get("average"))
    vwap_source = "quote_average" if vwap is not None else None
    if vwap is None:
        cum_volume = _as_float(mins[-1].get("cum_volume"))
        cum_amount = _as_float(mins[-1].get("cum_amount"))
        if cum_volume not in (None, 0) and cum_amount is not None:
            vwap = cum_amount / (cum_volume * 100.0)
            vwap_source = "tencent_cumulative"

    day_high = _as_float(quote.get("high"))
    day_low = _as_float(quote.get("low"))
    if day_high is None:
        day_high = max(float(x["price"]) for x in mins)
    if day_low is None:
        day_low = min(float(x["price"]) for x in mins)

    range_position = None
    if price is not None and day_high is not None and day_low is not None and day_high > day_low:
        range_position = (price - day_low) / (day_high - day_low) * 100.0

    recent15_high, recent15_low = _range_metrics(mins, 15)
    recent30_high, recent30_low = _range_metrics(mins, 30)
    activity = _activity_metrics(mins)
    structure = _structure_metrics(mins)

    status = "OK" if len(mins) >= 31 and price is not None else "PARTIAL"
    result = {
        "status": status,
        "minute_count": len(mins),
        "minute_last_time": mins[-1].get("time"),
        "price": price,
        "minute_price": minute_price,
        "quote_minute_price_gap_percent": _round(_pct_change(quote_price, minute_price), 4),
        "vwap": _round(vwap, 4),
        "vwap_source": vwap_source,
        "price_vs_vwap_percent": _round(_pct_change(price, vwap), 4),
        "above_vwap": None if price is None or vwap is None else price >= vwap,
        "day_high": day_high,
        "day_low": day_low,
        "from_day_high_percent": _round(_pct_change(price, day_high), 4),
        "from_day_low_percent": _round(_pct_change(price, day_low), 4),
        "day_range_position_percent": _round(range_position, 2),
        "trend_5m_percent": _round(_trend(mins, 5), 4),
        "trend_15m_percent": _round(_trend(mins, 15), 4),
        "trend_30m_percent": _round(_trend(mins, 30), 4),
        "recent_15m_high": recent15_high,
        "recent_15m_low": recent15_low,
        "distance_to_recent_15m_high_percent": _round(_pct_change(price, recent15_high), 4),
        "from_recent_15m_low_percent": _round(_pct_change(price, recent15_low), 4),
        "recent_30m_high": recent30_high,
        "recent_30m_low": recent30_low,
        "distance_to_recent_30m_high_percent": _round(_pct_change(price, recent30_high), 4),
        "from_recent_30m_low_percent": _round(_pct_change(price, recent30_low), 4),
    }
    result.update(activity)
    result.update(structure)
    return result


def install(base):
    minute_cache = {}
    original_tencent_minutes = base.tencent_minutes
    original_detail_payload = base.detail_payload

    def cached_tencent_minutes(tcode):
        date, rows = original_tencent_minutes(tcode)
        minute_cache[tcode] = (date, rows)
        return date, rows

    def detail_payload_with_intraday(now, code):
        result = original_detail_payload(now, code)
        try:
            _, _, tcode = base.infer_identifiers(code)
            cached = minute_cache.get(tcode)
            mins = base.parse_minutes(cached[1]) if cached else []
            result["intraday"] = build_intraday_metrics(result.get("quote"), mins)
        except Exception as exc:
            result["intraday"] = {
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {exc}",
            }
        return result

    base.tencent_minutes = cached_tencent_minutes
    base.detail_payload = detail_payload_with_intraday


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 4
    data.setdefault("features", {})["intraday_structure_metrics"] = "v1"

    errors = []
    for code, item in data.get("detail_stocks", {}).items():
        minutes = item.get("minutes") or {}
        intraday = item.get("intraday") or {}
        if (minutes.get("count") or 0) > 0 and not intraday:
            errors.append(f"{code}: missing intraday metrics")
            continue

        pos = _as_float(intraday.get("day_range_position_percent"))
        if pos is not None and not (-1.0 <= pos <= 101.0):
            errors.append(f"{code}: invalid day range position {pos}")

        if (minutes.get("count") or 0) >= 31 and intraday.get("trend_30m_percent") is None:
            errors.append(f"{code}: missing 30m trend")

        print(
            "INTRADAY "
            f"{code} status={intraday.get('status')} "
            f"price={intraday.get('price')} vwap={intraday.get('vwap')} "
            f"vs_vwap={intraday.get('price_vs_vwap_percent')}% "
            f"trend5/15/30={intraday.get('trend_5m_percent')}/"
            f"{intraday.get('trend_15m_percent')}/{intraday.get('trend_30m_percent')}% "
            f"day_pos={intraday.get('day_range_position_percent')}% "
            f"vol1m={intraday.get('volume_spike_ratio_1m')}x "
            f"vol5m={intraday.get('volume_strength_ratio_5m')}x "
            f"structure={intraday.get('structure')} bias={intraday.get('bias')}",
            flush=True,
        )

    if errors:
        raise RuntimeError("intraday metric validation failed: " + "; ".join(errors))

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"SNAPSHOT_SCHEMA_UPGRADED schema_version=4 feature=intraday_structure_metrics:v1", flush=True)
