import json
import math
import os
import statistics
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import data_metadata
import history_store


CAPITAL_FLOW_VERSION = "v1"
DATA_CLASSES = {
    "observed": "OBSERVED",
    "derived": "DERIVED",
    "official_delayed": "OFFICIAL_DELAYED",
    "vendor_estimate": "VENDOR_ESTIMATE",
}
MARGIN_REPORT = "RPTA_WEB_RZRQ_GGMX"
MARGIN_CACHE_SCHEMA = 1
MARGIN_KEEP_RECORDS = 24

_MINUTE_CACHE = {}


def _as_float(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value, digits=4):
    return None if value is None else round(float(value), digits)


def _ratio(a, b):
    a = _as_float(a)
    b = _as_float(b)
    if a is None or b in (None, 0):
        return None
    return a / b


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


def _clip(value, low=-1.0, high=1.0):
    return max(low, min(high, float(value)))


def _complete_minutes(mins):
    # The newest Tencent minute may still be accumulating. Use only completed
    # bars for amount/volume structure whenever possible.
    return list(mins[:-1] if len(mins) >= 2 else mins)


def _window_stats(mins, count):
    bars = _complete_minutes(mins)
    if not bars:
        return {
            "minutes": 0,
            "amount": None,
            "volume": None,
            "amount_rate_per_minute": None,
            "volume_rate_per_minute": None,
        }
    selected = bars[-min(count, len(bars)) :]
    amount = sum(max(0.0, _as_float(x.get("delta_amount")) or 0.0) for x in selected)
    volume = sum(max(0.0, _as_float(x.get("delta_volume")) or 0.0) for x in selected)
    n = len(selected)
    return {
        "minutes": n,
        "amount": _round(amount, 2),
        "volume": _round(volume, 2),
        "amount_rate_per_minute": _round(amount / n, 2) if n else None,
        "volume_rate_per_minute": _round(volume / n, 2) if n else None,
    }


def _turnover(mins, quote, previous_capital=None):
    bars = _complete_minutes(mins)
    windows = {str(n): _window_stats(mins, n) for n in (1, 5, 15, 30)}
    amount_today_minute = _as_float(bars[-1].get("cum_amount")) if bars else None
    volume_today_minute = _as_float(bars[-1].get("cum_volume")) if bars else None
    amount_today_quote = _as_float((quote or {}).get("amount_raw"))
    volume_today_quote = _as_float((quote or {}).get("volume_raw"))

    ref_index = len(bars)
    recent5 = bars[max(0, ref_index - 5) : ref_index]
    prior = bars[max(0, ref_index - 25) : max(0, ref_index - 5)]
    recent_rate = _mean([_as_float(x.get("delta_amount")) for x in recent5])
    baseline_rate = _mean([_as_float(x.get("delta_amount")) for x in prior])
    strength = _ratio(recent_rate, baseline_rate)

    prior5 = bars[max(0, ref_index - 10) : max(0, ref_index - 5)]
    prior5_rate = _mean([_as_float(x.get("delta_amount")) for x in prior5])
    acceleration = _ratio(recent_rate, prior5_rate)

    previous_rate = _as_float(
        ((((previous_capital or {}).get("observed") or {}).get("turnover") or {}).get("amount_rate_5m"))
    )
    acceleration_vs_previous = _ratio(recent_rate, previous_rate)

    quote_gap = None
    if amount_today_minute is not None and amount_today_quote not in (None, 0):
        quote_gap = (amount_today_minute / amount_today_quote - 1.0) * 100.0

    return {
        "amount_today": _round(amount_today_minute if amount_today_minute is not None else amount_today_quote, 2),
        "volume_today": _round(volume_today_minute if volume_today_minute is not None else volume_today_quote, 2),
        "amount_today_minute": _round(amount_today_minute, 2),
        "amount_today_quote": _round(amount_today_quote, 2),
        "amount_source_gap_percent": _round(quote_gap, 3),
        "amount_1m": windows["1"]["amount"],
        "amount_5m": windows["5"]["amount"],
        "amount_15m": windows["15"]["amount"],
        "amount_30m": windows["30"]["amount"],
        "volume_1m": windows["1"]["volume"],
        "volume_5m": windows["5"]["volume"],
        "volume_15m": windows["15"]["volume"],
        "volume_30m": windows["30"]["volume"],
        "amount_rate_1m": windows["1"]["amount_rate_per_minute"],
        "amount_rate_5m": windows["5"]["amount_rate_per_minute"],
        "amount_rate_15m": windows["15"]["amount_rate_per_minute"],
        "amount_rate_30m": windows["30"]["amount_rate_per_minute"],
        "amount_rate_baseline_20m": _round(baseline_rate, 2),
        "amount_rate_vs_baseline": _round(strength, 3),
        "turnover_acceleration": _round(acceleration, 3),
        "amount_rate_vs_previous_snapshot": _round(acceleration_vs_previous, 3),
        "baseline_minutes": len(prior),
        "previous_snapshot_comparable": previous_rate is not None,
    }


def _structure_for_window(mins):
    bars = _complete_minutes(mins)
    up_amount = down_amount = flat_amount = 0.0
    up_volume = down_volume = flat_volume = 0.0
    classified = 0
    for i, bar in enumerate(bars):
        if i == 0:
            continue
        previous_price = _as_float(bars[i - 1].get("price"))
        price = _as_float(bar.get("price"))
        if previous_price is None or price is None:
            continue
        amount = max(0.0, _as_float(bar.get("delta_amount")) or 0.0)
        volume = max(0.0, _as_float(bar.get("delta_volume")) or 0.0)
        classified += 1
        if price > previous_price:
            up_amount += amount
            up_volume += volume
        elif price < previous_price:
            down_amount += amount
            down_volume += volume
        else:
            flat_amount += amount
            flat_volume += volume
    total_amount = up_amount + down_amount + flat_amount
    total_volume = up_volume + down_volume + flat_volume
    return {
        "classified_minutes": classified,
        "up_amount": _round(up_amount, 2),
        "down_amount": _round(down_amount, 2),
        "flat_amount": _round(flat_amount, 2),
        "up_volume": _round(up_volume, 2),
        "down_volume": _round(down_volume, 2),
        "flat_volume": _round(flat_volume, 2),
        "up_amount_share": _round(up_amount / total_amount, 4) if total_amount else None,
        "down_amount_share": _round(down_amount / total_amount, 4) if total_amount else None,
        "flat_amount_share": _round(flat_amount / total_amount, 4) if total_amount else None,
        "up_volume_share": _round(up_volume / total_volume, 4) if total_volume else None,
        "down_volume_share": _round(down_volume / total_volume, 4) if total_volume else None,
        "flat_volume_share": _round(flat_volume / total_volume, 4) if total_volume else None,
        "up_amount_vs_down_amount": _round(_ratio(up_amount, down_amount), 3),
        "up_volume_vs_down_volume": _round(_ratio(up_volume, down_volume), 3),
        "semantic_note": "Price-change-associated turnover; not true active buy/sell flow.",
    }


def _volume_structure(mins):
    bars = _complete_minutes(mins)
    return {
        "full_session": _structure_for_window(bars),
        "last_30m": _structure_for_window(bars[-31:]),
        "last_15m": _structure_for_window(bars[-16:]),
    }


def _vwap_distribution(mins, vwap):
    vwap = _as_float(vwap)
    bars = _complete_minutes(mins)
    if not bars or vwap in (None, 0):
        return {
            "status": "UNAVAILABLE",
            "vwap": vwap,
            "above_amount_share": None,
            "below_amount_share": None,
            "near_amount_share": None,
        }
    above = below = near = 0.0
    tolerance = 0.001
    for bar in bars:
        price = _as_float(bar.get("price"))
        amount = max(0.0, _as_float(bar.get("delta_amount")) or 0.0)
        if price is None:
            continue
        diff = price / vwap - 1.0
        if diff > tolerance:
            above += amount
        elif diff < -tolerance:
            below += amount
        else:
            near += amount
    total = above + below + near
    return {
        "status": "OK" if total else "PARTIAL",
        "vwap": _round(vwap, 4),
        "near_tolerance_percent": 0.1,
        "above_amount": _round(above, 2),
        "below_amount": _round(below, 2),
        "near_amount": _round(near, 2),
        "above_amount_share": _round(above / total, 4) if total else None,
        "below_amount_share": _round(below / total, 4) if total else None,
        "near_amount_share": _round(near / total, 4) if total else None,
    }


def _price_volume_confirmation(mins, turnover):
    bars = _complete_minutes(mins)
    price_change = None
    if len(bars) >= 2:
        lookback = min(15, len(bars) - 1)
        price_change = _pct_change(bars[-1].get("price"), bars[-1 - lookback].get("price"))
    strength = _as_float(turnover.get("amount_rate_vs_baseline"))
    price_threshold = 0.15
    expansion_threshold = 1.20
    contraction_threshold = 0.80
    if price_change is None or strength is None:
        state = "UNKNOWN"
        reasons = ["INSUFFICIENT_PRICE_OR_TURNOVER_BASELINE"]
    elif abs(price_change) < price_threshold:
        state = "NEUTRAL"
        reasons = ["PRICE_MOVE_BELOW_THRESHOLD"]
    elif price_change > 0 and strength >= expansion_threshold:
        state = "UP_VOLUME_EXPANSION"
        reasons = ["PRICE_UP", "TURNOVER_RATE_EXPANDED"]
    elif price_change > 0 and strength <= contraction_threshold:
        state = "UP_VOLUME_CONTRACTION"
        reasons = ["PRICE_UP", "TURNOVER_RATE_CONTRACTED"]
    elif price_change < 0 and strength >= expansion_threshold:
        state = "DOWN_VOLUME_EXPANSION"
        reasons = ["PRICE_DOWN", "TURNOVER_RATE_EXPANDED"]
    elif price_change < 0 and strength <= contraction_threshold:
        state = "DOWN_VOLUME_CONTRACTION"
        reasons = ["PRICE_DOWN", "TURNOVER_RATE_CONTRACTED"]
    else:
        state = "NEUTRAL"
        reasons = ["TURNOVER_RATE_WITHIN_NORMAL_BAND"]
    return {
        "state": state,
        "price_change_15m_percent": _round(price_change, 4),
        "amount_rate_vs_baseline": _round(strength, 3),
        "thresholds": {
            "price_move_percent": price_threshold,
            "volume_expansion_ratio": expansion_threshold,
            "volume_contraction_ratio": contraction_threshold,
        },
        "reason_codes": reasons,
    }


def _pressure(structure, intraday):
    recent = (structure or {}).get("last_30m") or {}
    up_share = _as_float(recent.get("up_amount_share"))
    down_share = _as_float(recent.get("down_amount_share"))
    vs_vwap = _as_float((intraday or {}).get("price_vs_vwap_percent"))
    trend15 = _as_float((intraday or {}).get("trend_15m_percent"))
    if up_share is None or down_share is None:
        return {
            "buying": None,
            "selling": None,
            "net_bias": "UNKNOWN",
            "confidence": "LOW",
            "evidence": [],
            "reason_codes": ["INSUFFICIENT_DIRECTIONAL_TURNOVER"],
            "semantic_note": "Derived directional pressure, not observed capital net inflow.",
        }
    directional = _clip(up_share - down_share)
    vwap_component = _clip((vs_vwap or 0.0) / 1.0)
    trend_component = _clip((trend15 or 0.0) / 1.0)
    net = 50.0 + 25.0 * directional + 10.0 * vwap_component + 15.0 * trend_component
    buying = max(0.0, min(100.0, net))
    selling = 100.0 - buying
    if buying >= 57:
        bias = "BUY"
    elif buying <= 43:
        bias = "SELL"
    else:
        bias = "BALANCED"
    classified = int(recent.get("classified_minutes") or 0)
    confidence = "HIGH" if classified >= 25 else "MEDIUM" if classified >= 12 else "LOW"
    return {
        "buying": _round(buying, 2),
        "selling": _round(selling, 2),
        "net_bias": bias,
        "confidence": confidence,
        "components": {
            "directional_amount_share_delta": _round(directional, 4),
            "price_vs_vwap_percent": _round(vs_vwap, 4),
            "trend_15m_percent": _round(trend15, 4),
        },
        "formula": "50 + 25*(up_amount_share-down_amount_share) + 10*clip(price_vs_vwap/1%) + 15*clip(trend15/1%)",
        "evidence": [
            {"metric": "up_amount_share", "value": up_share},
            {"metric": "down_amount_share", "value": down_share},
            {"metric": "price_vs_vwap_percent", "value": vs_vwap},
            {"metric": "trend_15m_percent", "value": trend15},
        ],
        "reason_codes": [f"NET_BIAS_{bias}", f"CONFIDENCE_{confidence}"],
        "semantic_note": "Derived directional pressure, not observed capital net inflow.",
    }


def _absorption(mins, intraday):
    bars = _complete_minutes(mins)[-30:]
    if len(bars) < 12:
        return {"state": "UNKNOWN", "score": None, "evidence": [], "reason_codes": ["INSUFFICIENT_MINUTES"]}
    prices = [_as_float(x.get("price")) for x in bars]
    if any(x is None for x in prices):
        return {"state": "UNKNOWN", "score": None, "evidence": [], "reason_codes": ["INVALID_PRICE_SERIES"]}
    low_index = min(range(len(prices)), key=lambda i: prices[i])
    if low_index < 2 or low_index >= len(prices) - 2:
        return {"state": "NONE", "score": 0.0, "evidence": [], "reason_codes": ["NO_COMPLETED_DOWN_THEN_RECOVERY_SEQUENCE"]}
    low = prices[low_index]
    pre_high = max(prices[: low_index + 1])
    current = prices[-1]
    drawdown = abs(_pct_change(low, pre_high) or 0.0)
    if pre_high <= low:
        recovery_ratio = 0.0
    else:
        recovery_ratio = max(0.0, min(1.0, (current - low) / (pre_high - low)))

    amounts = [max(0.0, _as_float(x.get("delta_amount")) or 0.0) for x in bars]
    low_amount = amounts[low_index]
    baseline_amount = _median(amounts[max(0, low_index - 10) : low_index])
    low_activity_ratio = _ratio(low_amount, baseline_amount) or 0.0
    no_new_low = min(prices[low_index + 1 :]) >= low
    above_vwap = bool((intraday or {}).get("above_vwap"))
    score = 50.0 * recovery_ratio
    if low_activity_ratio >= 1.2:
        score += 20.0
    if above_vwap:
        score += 20.0
    if no_new_low:
        score += 10.0
    if drawdown < 0.20:
        score *= 0.5

    if score >= 70:
        state = "STRONG"
    elif score >= 45:
        state = "MODERATE"
    elif score >= 20:
        state = "WEAK"
    else:
        state = "NONE"
    reasons = []
    if recovery_ratio >= 0.5:
        reasons.append("POST_LOW_RECOVERY")
    if low_activity_ratio >= 1.2:
        reasons.append("LOW_AREA_TURNOVER_EXPANSION")
    if above_vwap:
        reasons.append("PRICE_AT_OR_ABOVE_VWAP")
    if no_new_low:
        reasons.append("NO_NEW_LOW_AFTER_LOCAL_LOW")
    if not reasons:
        reasons.append("NO_CLEAR_ABSORPTION_EVIDENCE")
    return {
        "state": state,
        "score": _round(score, 2),
        "evidence": [
            {"metric": "drawdown_to_local_low_percent", "value": _round(drawdown, 4)},
            {"metric": "recovery_ratio", "value": _round(recovery_ratio, 4)},
            {"metric": "low_area_amount_vs_prior_median", "value": _round(low_activity_ratio, 3)},
            {"metric": "no_new_low_after_local_low", "value": no_new_low},
            {"metric": "above_vwap", "value": above_vwap},
        ],
        "reason_codes": reasons,
        "semantic_note": "Pattern-based absorption context; does not assert hidden institutional activity.",
    }


def _vwap_acceptance(distribution, intraday, mins):
    if (distribution or {}).get("status") not in {"OK", "PARTIAL"}:
        return {"state": "UNKNOWN", "evidence": [], "reason_codes": ["VWAP_DISTRIBUTION_UNAVAILABLE"]}
    above_share = _as_float(distribution.get("above_amount_share")) or 0.0
    below_share = _as_float(distribution.get("below_amount_share")) or 0.0
    vs_vwap = _as_float((intraday or {}).get("price_vs_vwap_percent"))
    bars = _complete_minutes(mins)[-10:]
    vwap = _as_float(distribution.get("vwap"))
    last_above = sum(1 for x in bars if vwap and (_as_float(x.get("price")) or 0) > vwap)
    last_below = sum(1 for x in bars if vwap and (_as_float(x.get("price")) or 0) < vwap)

    if vs_vwap is None:
        state = "UNKNOWN"
    elif abs(vs_vwap) <= 0.15 and abs(above_share - below_share) <= 0.15:
        state = "OSCILLATING_AROUND_VWAP"
    elif vs_vwap > 0 and above_share >= 0.58 and last_above >= max(1, len(bars) * 0.6):
        state = "ACCEPTED_ABOVE_VWAP"
    elif vs_vwap < 0 and below_share >= 0.58 and last_below >= max(1, len(bars) * 0.6):
        state = "ACCEPTED_BELOW_VWAP"
    elif vs_vwap >= 0 and last_above >= 6 and below_share > above_share:
        state = "RECLAIMING_VWAP"
    elif vs_vwap < 0 and last_below >= 6 and above_share > below_share:
        state = "REJECTED_AT_VWAP"
    else:
        state = "OSCILLATING_AROUND_VWAP"
    return {
        "state": state,
        "evidence": [
            {"metric": "above_amount_share", "value": _round(above_share, 4)},
            {"metric": "below_amount_share", "value": _round(below_share, 4)},
            {"metric": "price_vs_vwap_percent", "value": _round(vs_vwap, 4)},
            {"metric": "last_10_above_vwap_minutes", "value": last_above},
            {"metric": "last_10_below_vwap_minutes", "value": last_below},
        ],
        "reason_codes": [state],
    }


def _margin_cache_path(code):
    root = Path(os.environ.get("MARKET_HISTORY_DIR", ".market-data/history"))
    return root / "capital_flow" / "margin" / f"{code}.json"


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _first(row, names):
    for name in names:
        if row.get(name) not in (None, "", "-"):
            return row.get(name)
    return None


def _date_text(value):
    text = str(value or "")
    return text[:10] if len(text) >= 10 else None


def _normalize_margin_row(row):
    trade_date = _date_text(_first(row, ("TRADE_DATE", "DATE", "TRADEDATE")))
    if not trade_date:
        return None
    financing_balance = _as_float(_first(row, ("FIN_BALANCE", "RZYE", "FINANCING_BALANCE")))
    financing_buy = _as_float(_first(row, ("FIN_BUY_AMT", "RZMRE", "FINANCING_BUY")))
    financing_repay = _as_float(_first(row, ("FIN_REPAY_AMT", "RZCHE", "FINANCING_REPAY")))
    securities_balance = _as_float(_first(row, ("SEC_LENDING_BALANCE", "RQYE", "SECURITIES_LENDING_BALANCE")))
    margin_balance = _as_float(_first(row, ("MARGIN_BALANCE", "RZRQYE", "TOTAL_BALANCE")))
    return {
        "trade_date": trade_date,
        "financing_balance": financing_balance,
        "financing_buy_amount": financing_buy,
        "financing_repay_amount": financing_repay,
        "financing_net_buy_amount": _round(financing_buy - financing_repay, 2) if financing_buy is not None and financing_repay is not None else None,
        "securities_lending_balance": securities_balance,
        "margin_balance": margin_balance,
    }


def fetch_margin_history(base, code, limit=MARGIN_KEEP_RECORDS):
    params = {
        "reportName": MARGIN_REPORT,
        "columns": "ALL",
        "filter": f'(SCODE="{code}")',
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
        "pageNumber": "1",
        "pageSize": str(max(limit, 20)),
        "source": "WEB",
        "client": "WEB",
        "_": str(int(time.time() * 1000)),
    }
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
    obj = json.loads(base.http_get(url))
    rows = (((obj or {}).get("result") or {}).get("data") or [])
    normalized = [_normalize_margin_row(row) for row in rows if isinstance(row, dict)]
    normalized = [x for x in normalized if x]
    by_date = {x["trade_date"]: x for x in normalized}
    return [by_date[key] for key in sorted(by_date, reverse=True)][:limit], url


def _margin_change(records, index):
    if not records or len(records) <= index:
        return None
    return _round((_as_float(records[0].get("financing_balance")) or 0.0) - (_as_float(records[index].get("financing_balance")) or 0.0), 2)


def _margin_context(base, code, now, execution_mode, daily_context=None):
    cache_path = _margin_cache_path(code)
    cached = _load_json(cache_path) or {}
    records = cached.get("records") if isinstance(cached.get("records"), list) else []
    source_url = cached.get("source_url")
    fetched_at = cached.get("fetched_at")
    fresh_fetch = False
    error = None

    if execution_mode == "FULL":
        try:
            records, source_url = fetch_margin_history(base, code)
            fetched_at = now.isoformat(timespec="seconds")
            payload = {
                "schema_version": MARGIN_CACHE_SCHEMA,
                "code": code,
                "source": "Eastmoney",
                "source_tier": "PRIMARY_PROVIDER",
                "data_semantics": "Exchange-published margin data redistributed by a market-data provider",
                "fetched_at": fetched_at,
                "source_url": source_url,
                "records": records,
            }
            _write_json(cache_path, payload)
            fresh_fetch = True
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

    latest = records[0] if records else None
    if not latest:
        return {
            "status": "UNAVAILABLE",
            "data_class": DATA_CLASSES["official_delayed"],
            "source": "Eastmoney",
            "source_tier": "PRIMARY_PROVIDER",
            "as_of_trade_date": None,
            "fetched_at": fetched_at,
            "error": error or "NO_MARGIN_CACHE_OR_PROVIDER_DATA",
            "records_available": 0,
        }

    balance = _as_float(latest.get("financing_balance"))
    change_1d = _margin_change(records, 1)
    change_5d = _margin_change(records, 5)
    change_20d = _margin_change(records, 20)
    price_return_5d = None
    bars = ((daily_context or {}).get("bars_last_60") or []) if isinstance(daily_context, dict) else []
    if len(bars) >= 6:
        price_return_5d = _pct_change((bars[-1] or {}).get("close"), (bars[-6] or {}).get("close"))
    balance_change_5d_pct = None
    if len(records) > 5 and _as_float(records[5].get("financing_balance")) not in (None, 0):
        balance_change_5d_pct = _pct_change(balance, records[5].get("financing_balance"))
    if price_return_5d is None or balance_change_5d_pct is None:
        relation = "UNKNOWN"
    elif price_return_5d > 0 and balance_change_5d_pct > 0:
        relation = "PRICE_UP_FINANCING_UP"
    elif price_return_5d < 0 and balance_change_5d_pct < 0:
        relation = "PRICE_DOWN_FINANCING_DOWN"
    elif price_return_5d > 0 and balance_change_5d_pct < 0:
        relation = "PRICE_UP_FINANCING_DOWN"
    else:
        relation = "PRICE_DOWN_FINANCING_UP"

    return {
        "status": "OK" if fresh_fetch else "CACHED",
        "data_class": DATA_CLASSES["official_delayed"],
        "source": "Eastmoney",
        "source_tier": "PRIMARY_PROVIDER",
        "source_trust_note": "Trust B transport/provider; OFFICIAL_DELAYED describes disclosure semantics, not source authority.",
        "as_of_trade_date": latest.get("trade_date"),
        "fetched_at": fetched_at,
        "financing_balance": balance,
        "financing_buy_amount": latest.get("financing_buy_amount"),
        "financing_repay_amount": latest.get("financing_repay_amount"),
        "financing_net_buy_amount": latest.get("financing_net_buy_amount"),
        "securities_lending_balance": latest.get("securities_lending_balance"),
        "margin_balance": latest.get("margin_balance"),
        "financing_balance_change_1d": change_1d,
        "financing_balance_change_5d": change_5d,
        "financing_balance_change_20d": change_20d,
        "financing_balance_change_5d_percent": _round(balance_change_5d_pct, 4),
        "price_return_5d_percent": _round(price_return_5d, 4),
        "price_financing_relation_5d": relation,
        "records_available": len(records),
        "source_url": source_url,
        "cache_path": str(cache_path),
        "cache_only_fast_path": execution_mode != "FULL",
        "error": error,
    }


def _quote_map(snapshot):
    out = {}
    for code, item in (snapshot.get("detail_stocks") or {}).items():
        if isinstance(item, dict) and isinstance(item.get("quote"), dict):
            out[code] = item["quote"]
    for code, item in (snapshot.get("light_stocks") or {}).items():
        if isinstance(item, dict) and isinstance(item.get("quote"), dict):
            out[code] = item["quote"]
    return out


def _market_date(quote):
    value = str((quote or {}).get("market_time_cst") or "")
    return value[:10] if len(value) >= 10 else None


def _snapshot_time(snapshot):
    for key in ("runner_time_utc", "runner_time_cst"):
        value = snapshot.get(key)
        if not value:
            continue
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return dt
            return dt
        except ValueError:
            try:
                return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
    return None


def _peer_context(snapshot, previous, target_code):
    current_quotes = _quote_map(snapshot)
    previous_quotes = _quote_map(previous or {})
    current_time = _snapshot_time(snapshot)
    previous_time = _snapshot_time(previous or {})
    interval_minutes = None
    if current_time and previous_time:
        try:
            interval_minutes = (current_time - previous_time).total_seconds() / 60.0
        except TypeError:
            interval_minutes = None
    if interval_minutes is not None and interval_minutes <= 0:
        interval_minutes = None

    results = []
    for group_id, group in (snapshot.get("groups") or {}).items():
        target = ((group or {}).get("target") or {}).get("code")
        if target != target_code:
            continue
        members = [str(x.get("code")) for x in ((group or {}).get("members") or []) if isinstance(x, dict) and x.get("code")]
        universe = sorted(set(members + [target_code]))
        previous_group = ((previous or {}).get("groups") or {}).get(group_id) or {}
        previous_members = [str(x.get("code")) for x in (previous_group.get("members") or []) if isinstance(x, dict) and x.get("code")]
        previous_target = (previous_group.get("target") or {}).get("code")
        previous_universe = sorted(set(previous_members + ([previous_target] if previous_target else [])))
        universe_comparable = bool(previous_group and universe == previous_universe and previous_target == target_code)

        rates = []
        for code in universe:
            cur = current_quotes.get(code) or {}
            old = previous_quotes.get(code) or {}
            current_amount = _as_float(cur.get("amount_raw"))
            previous_amount = _as_float(old.get("amount_raw"))
            same_session = bool(_market_date(cur) and _market_date(cur) == _market_date(old))
            rate = None
            if same_session and interval_minutes and current_amount is not None and previous_amount is not None and current_amount >= previous_amount:
                rate = (current_amount - previous_amount) / interval_minutes
            price_delta = _pct_change(cur.get("latest"), old.get("latest")) if same_session else None
            rates.append({"code": code, "amount_rate_since_previous": rate, "price_delta_percent": price_delta})

        available = [x for x in rates if x["amount_rate_since_previous"] is not None]
        median_rate = _median([x["amount_rate_since_previous"] for x in available])
        ranked = sorted(available, key=lambda x: x["amount_rate_since_previous"], reverse=True)
        target_entry = next((x for x in rates if x["code"] == target_code), None)
        rank = next((i + 1 for i, x in enumerate(ranked) if x["code"] == target_code), None)
        target_rate = (target_entry or {}).get("amount_rate_since_previous")
        relative = _ratio(target_rate, median_rate)

        active_up = active_down = 0
        for entry in available:
            if median_rate is None or entry["amount_rate_since_previous"] < median_rate:
                continue
            delta = entry.get("price_delta_percent")
            if delta is not None and delta > 0:
                active_up += 1
            elif delta is not None and delta < 0:
                active_down += 1
        active_total = active_up + active_down
        dominant = max(active_up, active_down) / active_total if active_total else None
        if dominant is None:
            sync = "UNKNOWN"
        elif dominant >= 0.75:
            sync = "STRONG"
        elif dominant >= 0.60:
            sync = "MODERATE"
        else:
            sync = "WEAK"
        direction = "UP" if active_up > active_down else "DOWN" if active_down > active_up else "MIXED"

        results.append({
            "group_id": group_id,
            "peer_universe": universe,
            "peer_universe_signature": "|".join(universe),
            "previous_peer_universe_signature": "|".join(previous_universe) if previous_universe else None,
            "comparability": {
                "comparable_to_previous": universe_comparable,
                "reason": "SAME_PEER_UNIVERSE" if universe_comparable else "PEER_UNIVERSE_CHANGED_OR_BASELINE_MISSING",
            },
            "interval_minutes": _round(interval_minutes, 3),
            "relative_capital_strength": _round(relative, 3),
            "target_amount_rate_since_previous": _round(target_rate, 2),
            "peer_median_amount_rate_since_previous": _round(median_rate, 2),
            "rank": rank,
            "peer_count": len(ranked),
            "sector_sync": sync,
            "sector_active_direction": direction,
            "active_up_count": active_up,
            "active_down_count": active_down,
            "method": "same-session cumulative turnover delta per minute versus previous snapshot",
        })
    return {
        "status": "OK" if results else "UNAVAILABLE",
        "groups": results,
        "primary": results[0] if results else None,
    }


def _build_metadata(now_iso, minutes, margin, peer, quality):
    minute_freshness = (minutes or {}).get("freshness") or "UNKNOWN"
    minute_lag = (minutes or {}).get("lag_seconds")
    observed_meta = data_metadata._metadata(
        (minutes or {}).get("source") or "Tencent",
        now_iso,
        data_time=None,
        lag_seconds=minute_lag,
        freshness=minute_freshness,
        freshness_policy="INTRADAY_FUND_FLOW",
        quality="PASS" if minute_freshness in {"LIVE", "CURRENT_SESSION", "LAST_SESSION"} else "DEGRADED",
        source_tier="SECONDARY_PROVIDER",
        data_class="INTRADAY_FUND_FLOW",
    )
    derived_meta = data_metadata._metadata(
        "DERIVED",
        now_iso,
        data_time=None,
        freshness="DERIVED_CURRENT",
        freshness_policy="DERIVED",
        quality=quality,
        source_type="DERIVED",
        source_tier="DERIVED",
        data_class="DERIVED",
    )
    margin_status = str((margin or {}).get("status") or "UNAVAILABLE")
    margin_quality = "PASS" if margin_status == "OK" else "DEGRADED" if margin_status == "CACHED" else "PARTIAL"
    margin_meta = data_metadata._metadata(
        (margin or {}).get("source") or "Eastmoney",
        (margin or {}).get("fetched_at") or now_iso,
        data_time=(margin or {}).get("as_of_trade_date"),
        freshness="LATEST_AVAILABLE_SESSION" if (margin or {}).get("as_of_trade_date") else "UNAVAILABLE",
        freshness_policy="DAILY_FINANCING",
        quality=margin_quality,
        quality_flags=["OFFICIAL_DISCLOSURE_SEMANTICS_VIA_VENDOR"] + (["FAST_CACHE_ONLY"] if (margin or {}).get("cache_only_fast_path") else []),
        source_tier="PRIMARY_PROVIDER",
        data_class="DAILY_FINANCING",
        session_verified=margin_status == "OK",
        completed_session_age=0 if margin_status == "OK" else None,
        session_validation_state="PROVIDER_LATEST_AVAILABLE" if margin_status == "OK" else "CACHE_ONLY_UNVERIFIED",
    )
    peer_meta = data_metadata._metadata(
        "DERIVED",
        now_iso,
        freshness="DERIVED_CURRENT",
        freshness_policy="DERIVED",
        quality="PASS" if (peer or {}).get("status") == "OK" else "PARTIAL",
        source_type="DERIVED",
        source_tier="DERIVED",
        data_class="DERIVED",
    )
    return observed_meta, derived_meta, margin_meta, peer_meta


def build_capital_flow(code, item, mins, previous, snapshot, base, now, execution_mode):
    quote = item.get("quote") or {}
    minutes = item.get("minutes") or {}
    intraday = item.get("intraday") or {}
    previous_item = ((previous or {}).get("detail_stocks") or {}).get(code) or {}
    previous_capital = previous_item.get("capital_flow") or {}
    turnover = _turnover(mins, quote, previous_capital)
    structure = _volume_structure(mins)
    distribution = _vwap_distribution(mins, intraday.get("vwap") or quote.get("average"))
    confirmation = _price_volume_confirmation(mins, turnover)
    pressure = _pressure(structure, intraday)
    absorption = _absorption(mins, intraday)
    vwap_acceptance = _vwap_acceptance(distribution, intraday, mins)
    margin = _margin_context(base, code, now, execution_mode, item.get("daily_context"))
    peer = _peer_context(snapshot, previous, code)

    minute_count = len(_complete_minutes(mins))
    if minute_count < 5:
        status = "PARTIAL"
        quality = "PARTIAL"
        flags = ["INSUFFICIENT_MINUTE_DATA"]
    elif margin.get("status") == "UNAVAILABLE" or peer.get("status") != "OK":
        status = "DEGRADED"
        quality = "DEGRADED"
        flags = []
        if margin.get("status") == "UNAVAILABLE":
            flags.append("MARGIN_DATA_UNAVAILABLE")
        if peer.get("status") != "OK":
            flags.append("PEER_CONTEXT_UNAVAILABLE")
    else:
        status = "OK"
        quality = "PASS"
        flags = []

    now_iso = now.isoformat(timespec="seconds")
    observed_meta, derived_meta, margin_meta, peer_meta = _build_metadata(now_iso, minutes, margin, peer, quality)
    turnover["metadata"] = observed_meta
    turnover["provenance"] = {
        "type": DATA_CLASSES["observed"],
        "derived_from": [f"detail_stocks.{code}.minutes", f"detail_stocks.{code}.quote"],
        "method": "minute cumulative turnover differencing; newest incomplete minute excluded",
    }
    structure["metadata"] = observed_meta
    structure["provenance"] = {
        "type": DATA_CLASSES["observed"],
        "derived_from": [f"detail_stocks.{code}.minutes"],
        "method": "classify each completed minute by close-to-close price direction and attach that minute turnover",
    }
    distribution["metadata"] = observed_meta
    distribution["provenance"] = {
        "type": DATA_CLASSES["observed"],
        "derived_from": [f"detail_stocks.{code}.minutes", f"detail_stocks.{code}.intraday.vwap"],
        "method": "minute turnover distribution around VWAP",
    }
    for value, algorithm in (
        (confirmation, "price_volume_confirmation_v1"),
        (pressure, "directional_pressure_v1"),
        (absorption, "absorption_pattern_v1"),
        (vwap_acceptance, "vwap_acceptance_v1"),
    ):
        value["metadata"] = derived_meta
        value["provenance"] = {
            "type": DATA_CLASSES["derived"],
            "derived_from": [
                f"detail_stocks.{code}.capital_flow.observed",
                f"detail_stocks.{code}.intraday",
            ],
            "algorithm": algorithm,
        }
    margin["metadata"] = margin_meta
    margin["provenance"] = {
        "type": DATA_CLASSES["official_delayed"],
        "provider": "Eastmoney",
        "underlying_semantics": "exchange-published margin financing dataset",
        "source_authority": "Trust B provider, not direct exchange endpoint",
    }
    peer["metadata"] = peer_meta
    peer["provenance"] = {
        "type": DATA_CLASSES["derived"],
        "derived_from": ["current quotes", "previous exact snapshot", "configured group universe"],
        "algorithm": "same_session_turnover_rate_peer_comparison_v1",
    }

    metadata = data_metadata._metadata(
        "DERIVED",
        now_iso,
        freshness="DERIVED_CURRENT",
        freshness_policy="DERIVED",
        quality=quality,
        quality_flags=flags,
        source_type="DERIVED",
        source_tier="DERIVED",
        data_class="DERIVED",
    )
    return {
        "status": status,
        "version": CAPITAL_FLOW_VERSION,
        "data_class_contract": DATA_CLASSES,
        "observed": {
            "turnover": turnover,
            "volume_structure": structure,
            "vwap_distribution": distribution,
        },
        "derived": {
            "price_volume_confirmation": confirmation,
            "pressure": pressure,
            "absorption": absorption,
            "vwap_acceptance": vwap_acceptance,
        },
        "official_delayed": {"margin": margin},
        "vendor_estimate": {
            "status": "NOT_USED",
            "data_class": DATA_CLASSES["vendor_estimate"],
            "reason": "No vendor-estimated main-force/order-size flow is required for v1; observed/derived facts remain primary.",
        },
        "peer_context": peer,
        "metadata": metadata,
        "provenance": {
            "type": "COMPOSITE",
            "derived_from": [
                f"detail_stocks.{code}.quote",
                f"detail_stocks.{code}.minutes",
                f"detail_stocks.{code}.intraday",
                f"detail_stocks.{code}.daily_context",
                "previous exact snapshot",
                "Eastmoney margin dataset/cache",
            ],
        },
    }


def install(base):
    if getattr(base, "_capital_flow_capture_installed", False):
        return
    original_tencent_minutes = base.tencent_minutes

    def captured_minutes(tcode):
        date, rows = original_tencent_minutes(tcode)
        _MINUTE_CACHE[tcode] = (date, list(rows or []))
        return date, rows

    base.tencent_minutes = captured_minutes
    base._capital_flow_capture_installed = True


def finalize_snapshot(snapshot_path, base, execution_mode):
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    previous, previous_path = history_store.load_previous_snapshot(snapshot)
    now_text = snapshot.get("runner_time_cst") or snapshot.get("runner_time_utc")
    try:
        now = datetime.fromisoformat(str(now_text).replace("Z", "+00:00"))
    except Exception:
        now = datetime.now(base.CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=base.CST)

    statuses = {}
    for code, item in (snapshot.get("detail_stocks") or {}).items():
        _, _, tcode = base.infer_identifiers(code)
        cached = _MINUTE_CACHE.get(tcode)
        mins = base.parse_minutes(cached[1]) if cached else []
        capital = build_capital_flow(code, item, mins, previous, snapshot, base, now, execution_mode)
        capital["previous_snapshot_path"] = previous_path
        item["capital_flow"] = capital
        statuses[code] = capital.get("status")
        pressure = ((capital.get("derived") or {}).get("pressure") or {})
        absorption = ((capital.get("derived") or {}).get("absorption") or {})
        confirmation = ((capital.get("derived") or {}).get("price_volume_confirmation") or {})
        turnover = ((capital.get("observed") or {}).get("turnover") or {})
        print(
            "CAPITAL_FLOW "
            f"{code} status={capital.get('status')} "
            f"rate5={turnover.get('amount_rate_5m')} "
            f"rate_vs_base={turnover.get('amount_rate_vs_baseline')} "
            f"pv={confirmation.get('state')} pressure={pressure.get('net_bias')} "
            f"absorption={absorption.get('state')} margin={((capital.get('official_delayed') or {}).get('margin') or {}).get('status')}",
            flush=True,
        )

    snapshot["schema_version"] = max(int(snapshot.get("schema_version") or 0), 14)
    snapshot.setdefault("features", {})["capital_flow"] = CAPITAL_FLOW_VERSION
    snapshot["capital_flow_summary"] = {
        "status": "OK" if statuses and all(x == "OK" for x in statuses.values()) else "PARTIAL" if statuses else "UNAVAILABLE",
        "detail_stock_count": len(statuses),
        "status_by_code": statuses,
        "data_class_contract": DATA_CLASSES,
        "vendor_estimate_policy": "OPTIONAL_AND_NON_DOMINANT",
        "fast_path_margin_policy": "CACHE_ONLY_NO_NETWORK",
    }
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print("SNAPSHOT_SCHEMA_UPGRADED schema_version=14 feature=capital_flow:v1", flush=True)
