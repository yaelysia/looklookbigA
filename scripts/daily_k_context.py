import json
import math
import statistics
import time
import urllib.parse
from datetime import datetime
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
    vals = [float(v) for v in values if v is not None]
    return statistics.fmean(vals) if vals else None


def _normalize_bar(row, source):
    return {
        "date": str(row[0]),
        "open": float(row[1]),
        "close": float(row[2]),
        "high": float(row[3]),
        "low": float(row[4]),
        "volume": float(row[5]) if len(row) > 5 and row[5] not in (None, "") else None,
        "amount": float(row[6]) if len(row) > 6 and row[6] not in (None, "") else None,
        "source": source,
    }


def _fetch_tencent(base, code, limit=90):
    _, _, tcode = base.infer_identifiers(code)
    params = {"param": f"{tcode},day,,,{int(limit)},qfq", "_t": str(int(time.time() * 1000))}
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urllib.parse.urlencode(params)
    obj = json.loads(base.http_get(url, timeout=10, attempts=2))
    node = (obj.get("data") or {}).get(tcode) or {}
    rows = node.get("qfqday") or node.get("day") or []
    bars = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            # Tencent day rows are date, open, close, high, low, volume.
            bars.append(_normalize_bar(row[:6], "Tencent qfq"))
        except (TypeError, ValueError):
            continue
    if not bars:
        raise RuntimeError("Tencent daily K returned no parsable bars")
    return bars


def _fetch_eastmoney(base, code, limit=90):
    _, secid, _ = base.infer_identifiers(code)
    params = {
        "secid": secid,
        "klt": "101",
        "fqt": "1",
        "lmt": str(int(limit)),
        "end": "20500101",
        "iscca": "1",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "_": str(int(time.time() * 1000)),
    }
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urllib.parse.urlencode(params)
    obj = json.loads(base.http_get(url, timeout=10, attempts=2))
    rows = ((obj.get("data") or {}).get("klines") or [])
    bars = []
    for line in rows:
        fields = str(line).split(",")
        if len(fields) < 7:
            continue
        try:
            # Eastmoney: date,open,close,high,low,volume,amount,...
            bars.append(_normalize_bar(fields[:7], "Eastmoney qfq"))
        except (TypeError, ValueError):
            continue
    if not bars:
        raise RuntimeError("Eastmoney daily K returned no parsable bars")
    return bars


def fetch_daily_bars(base, code, limit=90):
    errors = []
    for source, func in (("Tencent qfq", _fetch_tencent), ("Eastmoney qfq", _fetch_eastmoney)):
        try:
            bars = func(base, code, limit)
            bars = sorted({bar["date"]: bar for bar in bars}.values(), key=lambda x: x["date"])
            return source, bars, errors
        except Exception as exc:
            errors.append(f"{source}: {type(exc).__name__}: {exc}")
    raise RuntimeError("; ".join(errors))


def _split_completed_bars(now, bars):
    today = now.strftime("%Y-%m-%d")
    current_partial = None
    completed = []
    today_complete = (now.hour, now.minute) >= (15, 5)
    for bar in bars:
        if bar["date"] == today and not today_complete:
            current_partial = bar
            continue
        completed.append(bar)
    return completed, current_partial


def _ma(bars, window):
    if len(bars) < window:
        return None
    return _mean([bar["close"] for bar in bars[-window:]])


def _atr(bars, window=14):
    if len(bars) < window + 1:
        return None
    trs = []
    for idx in range(len(bars) - window, len(bars)):
        bar = bars[idx]
        prev_close = bars[idx - 1]["close"]
        tr = max(
            bar["high"] - bar["low"],
            abs(bar["high"] - prev_close),
            abs(bar["low"] - prev_close),
        )
        trs.append(tr)
    return _mean(trs)


def _range_context(bars, window):
    if not bars:
        return None
    subset = bars[-min(window, len(bars)) :]
    high_bar = max(subset, key=lambda x: x["high"])
    low_bar = min(subset, key=lambda x: x["low"])
    return {
        "window": min(window, len(subset)),
        "high": high_bar["high"],
        "high_date": high_bar["date"],
        "low": low_bar["low"],
        "low_date": low_bar["date"],
    }


def _daily_swings(bars, lookback=60, radius=2):
    data = bars[-min(lookback, len(bars)) :]
    highs = []
    lows = []
    if len(data) < radius * 2 + 1:
        return highs, lows
    for i in range(radius, len(data) - radius):
        window = data[i - radius : i + radius + 1]
        cur = data[i]
        highs_in_window = [x["high"] for x in window]
        lows_in_window = [x["low"] for x in window]
        if cur["high"] == max(highs_in_window) and highs_in_window.index(cur["high"]) == radius:
            highs.append({"date": cur["date"], "price": cur["high"]})
        if cur["low"] == min(lows_in_window) and lows_in_window.index(cur["low"]) == radius:
            lows.append({"date": cur["date"], "price": cur["low"]})
    return highs, lows


def _ma_alignment(mas):
    ma5, ma10, ma20, ma60 = (mas.get(f"ma{n}") for n in (5, 10, 20, 60))
    if None not in (ma5, ma10, ma20, ma60):
        if ma5 > ma10 > ma20 > ma60:
            return "BULLISH_STACK"
        if ma5 < ma10 < ma20 < ma60:
            return "BEARISH_STACK"
    if None not in (ma5, ma10, ma20):
        if ma5 > ma10 > ma20:
            return "SHORT_BULLISH"
        if ma5 < ma10 < ma20:
            return "SHORT_BEARISH"
    return "MIXED"


def _source_weight(source):
    if source.startswith("SWING_"):
        return 1.8
    if source in ("20D_LOW", "20D_HIGH"):
        return 1.6
    if source in ("10D_LOW", "10D_HIGH", "MA20"):
        return 1.3
    if source in ("5D_LOW", "5D_HIGH", "MA10", "MA60"):
        return 1.1
    if source in ("PREV_LOW", "PREV_HIGH"):
        return 1.0
    if source == "MA5":
        return 0.9
    if source == "PREV_CLOSE":
        return 0.7
    return 1.0


def _cluster_levels(levels, tolerance):
    if not levels:
        return []
    levels = sorted(levels, key=lambda x: x["price"])
    clusters = []
    for item in levels:
        if not clusters or abs(item["price"] - clusters[-1]["center"]) > tolerance:
            clusters.append({"items": [item], "center": item["price"]})
            continue
        cluster = clusters[-1]
        cluster["items"].append(item)
        weights = [_source_weight(x["source"]) for x in cluster["items"]]
        cluster["center"] = sum(x["price"] * w for x, w in zip(cluster["items"], weights)) / sum(weights)
    return clusters


def _level_strength(score):
    if score >= 4.5:
        return "STRONG"
    if score >= 2.5:
        return "MEDIUM"
    return "WEAK"


def _build_key_levels(current_price, atr14, mas, ranges, prev, swings):
    if current_price is None:
        return {"status": "NO_CURRENT_PRICE", "supports": [], "resistances": []}

    raw = []
    for key, label in (("ma5", "MA5"), ("ma10", "MA10"), ("ma20", "MA20"), ("ma60", "MA60")):
        if mas.get(key) is not None:
            raw.append({"price": mas[key], "source": label})

    for days, context in ranges.items():
        if context:
            raw.append({"price": context["low"], "source": f"{days}D_LOW"})
            raw.append({"price": context["high"], "source": f"{days}D_HIGH"})

    if prev:
        raw.extend(
            [
                {"price": prev["low"], "source": "PREV_LOW"},
                {"price": prev["high"], "source": "PREV_HIGH"},
                {"price": prev["close"], "source": "PREV_CLOSE"},
            ]
        )

    for point in swings.get("lows", [])[-3:]:
        raw.append({"price": point["price"], "source": f"SWING_LOW:{point['date']}"})
    for point in swings.get("highs", [])[-3:]:
        raw.append({"price": point["price"], "source": f"SWING_HIGH:{point['date']}"})

    tolerance = max(current_price * 0.0035, (atr14 or 0) * 0.12)
    clusters = _cluster_levels(raw, tolerance)
    supports = []
    resistances = []

    for cluster in clusters:
        items = cluster["items"]
        price = cluster["center"]
        score = sum(_source_weight(x["source"]) for x in items)
        entry = {
            "price": _round(price, 3),
            "distance_percent": _round(_pct_change(price, current_price), 3),
            "distance_atr": _round((price - current_price) / atr14, 3) if atr14 not in (None, 0) else None,
            "score": _round(score, 2),
            "strength": _level_strength(score),
            "sources": [x["source"] for x in items],
        }
        if price <= current_price:
            supports.append(entry)
        else:
            resistances.append(entry)

    supports.sort(key=lambda x: x["price"], reverse=True)
    resistances.sort(key=lambda x: x["price"])
    return {
        "status": "OK",
        "cluster_tolerance": _round(tolerance, 4),
        "supports": supports[:5],
        "resistances": resistances[:5],
    }


def build_daily_context(base, now, code, detail_item):
    result = {
        "status": "ERROR",
        "source": None,
        "errors": [],
        "history_count": 0,
        "completed_bar_count": 0,
        "latest_completed_date": None,
    }
    try:
        source, bars, source_errors = fetch_daily_bars(base, code, limit=90)
        completed, current_partial = _split_completed_bars(now, bars)
        completed = completed[-60:]
        if len(completed) < 20:
            raise RuntimeError(f"only {len(completed)} completed daily bars")

        quote = detail_item.get("quote") or {}
        intraday = detail_item.get("intraday") or {}
        minutes = detail_item.get("minutes") or {}
        current_price = _as_float(quote.get("latest"))
        if current_price is None:
            current_price = _as_float(intraday.get("price"))
        if current_price is None:
            current_price = _as_float(minutes.get("last_price"))

        mas = {f"ma{n}": _round(_ma(completed, n), 4) for n in (5, 10, 20, 60)}
        atr14 = _atr(completed, 14)
        ranges = {n: _range_context(completed, n) for n in (5, 10, 20)}
        prev = completed[-1]
        swing_highs, swing_lows = _daily_swings(completed, lookback=60, radius=2)
        swings = {"highs": swing_highs, "lows": swing_lows}

        volume20 = _mean([bar["volume"] for bar in completed[-20:] if bar.get("volume") is not None])
        prev_volume_ratio = None
        if volume20 not in (None, 0) and prev.get("volume") is not None:
            prev_volume_ratio = prev["volume"] / volume20

        trend_return_20d = _pct_change(completed[-1]["close"], completed[-21]["close"]) if len(completed) >= 21 else None
        current_vs = {
            "ma5_percent": _round(_pct_change(current_price, mas["ma5"]), 4),
            "ma10_percent": _round(_pct_change(current_price, mas["ma10"]), 4),
            "ma20_percent": _round(_pct_change(current_price, mas["ma20"]), 4),
            "ma60_percent": _round(_pct_change(current_price, mas["ma60"]), 4),
            "prev_close_percent": _round(_pct_change(current_price, prev["close"]), 4),
            "20d_high_percent": _round(_pct_change(current_price, ranges[20]["high"]), 4),
            "20d_low_percent": _round(_pct_change(current_price, ranges[20]["low"]), 4),
        }

        result.update(
            {
                "status": "OK" if len(completed) >= 60 else "PARTIAL",
                "source": source,
                "errors": source_errors,
                "history_count": len(bars),
                "completed_bar_count": len(completed),
                "latest_completed_date": prev["date"],
                "current_partial_bar": current_partial,
                "current_price": current_price,
                "previous_day": prev,
                "moving_averages": mas,
                "ma_alignment": _ma_alignment(mas),
                "atr14": _round(atr14, 4),
                "atr14_percent_of_price": _round((atr14 / current_price * 100.0), 3)
                if atr14 is not None and current_price not in (None, 0)
                else None,
                "range_5d": ranges[5],
                "range_10d": ranges[10],
                "range_20d": ranges[20],
                "return_20d_percent": _round(trend_return_20d, 4),
                "previous_volume_vs_20d_mean": _round(prev_volume_ratio, 3),
                "current_vs_context": current_vs,
                "daily_swings": {
                    "last_swing_highs": swing_highs[-4:],
                    "last_swing_lows": swing_lows[-4:],
                },
                "key_levels": _build_key_levels(current_price, atr14, mas, ranges, prev, swings),
                "bars_last_60": completed,
            }
        )
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result


def install(base):
    original_detail_payload = base.detail_payload

    def detail_payload_with_daily_context(now, code):
        item = original_detail_payload(now, code)
        item["daily_context"] = build_daily_context(base, now, code, item)
        context = item["daily_context"]
        levels = context.get("key_levels") or {}
        support = (levels.get("supports") or [{}])[0].get("price") if levels.get("supports") else None
        resistance = (levels.get("resistances") or [{}])[0].get("price") if levels.get("resistances") else None
        print(
            f"DAILY {code} status={context.get('status')} source={context.get('source')} "
            f"bars={context.get('completed_bar_count')} ma20={((context.get('moving_averages') or {}).get('ma20'))} "
            f"atr14={context.get('atr14')} alignment={context.get('ma_alignment')} "
            f"support1={support} resistance1={resistance}",
            flush=True,
        )
        return item

    base.detail_payload = detail_payload_with_daily_context


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = max(5, int(data.get("schema_version") or 0))
    data.setdefault("features", {})["daily_k_context"] = "v1"

    errors = []
    for code, item in data.get("detail_stocks", {}).items():
        context = item.get("daily_context") or {}
        if not context:
            errors.append(f"{code}: missing daily_context")
            continue
        if context.get("status") in ("OK", "PARTIAL"):
            bars = context.get("completed_bar_count") or 0
            if bars < 20:
                errors.append(f"{code}: insufficient completed bars {bars}")
            ma20 = ((context.get("moving_averages") or {}).get("ma20"))
            if ma20 is None:
                errors.append(f"{code}: missing ma20")
            levels = context.get("key_levels") or {}
            if levels.get("status") == "OK" and not (levels.get("supports") or levels.get("resistances")):
                errors.append(f"{code}: no key levels")

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if errors:
        raise RuntimeError("daily context validation failed: " + "; ".join(errors))
    print("SNAPSHOT_SCHEMA_UPGRADED schema_version=5 feature=daily_k_context:v1", flush=True)
