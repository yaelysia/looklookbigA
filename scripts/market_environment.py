import json
import statistics
from pathlib import Path


CORE_INDICES = (
    "上证指数",
    "沪深300",
    "中证1000",
    "深证成指",
    "创业板指",
    "科创50",
)
ADDITIONAL_INDICES = (
    ("沪深300", "1.000300", "sh000300"),
    ("中证1000", "1.000852", "sh000852"),
    ("科创50", "1.000688", "sh000688"),
)
USABLE_FRESHNESS = {"LIVE", "CURRENT_SESSION", "LAST_SESSION"}
MARKET_UNIVERSE_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
MARKET_UNIVERSE_FIELDS = "f2,f3,f6,f12,f13,f14,f15,f18"
MARKET_UNIVERSE_PAGE_SIZE = 10000
LAST_BREADTH = None

REGIME_ZH = {
    "BROAD_RISK_ON": "普涨偏强",
    "BROAD_RISK_OFF": "普跌偏弱",
    "INDEX_UP_NARROW": "指数上涨但个股广度偏窄",
    "INDEX_DOWN_BREADTH_RESILIENT": "指数偏弱但个股广度相对抗跌",
    "ROTATION_MIXED": "结构分化/轮动",
    "BALANCED": "整体均衡",
    "UNKNOWN": "市场状态未知",
}

CONFIDENCE_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def _as_float(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _round(value, digits=4):
    return None if value is None else round(float(value), digits)


def _mean(values):
    values = [float(x) for x in values if x is not None]
    return statistics.fmean(values) if values else None


def _median(values):
    values = [float(x) for x in values if x is not None]
    return statistics.median(values) if values else None


def _min_confidence(*levels):
    normalized = [level if level in CONFIDENCE_RANK else "LOW" for level in levels]
    return min(normalized, key=lambda level: CONFIDENCE_RANK[level]) if normalized else "LOW"


def configure_indices(base, quote_resilience):
    existing = {name for name, _ in base.INDICES}
    for name, secid, tcode in ADDITIONAL_INDICES:
        if name not in existing:
            base.INDICES.append((name, secid))
            existing.add(name)
        quote_resilience.INDEX_TENCENT_CODES.setdefault(name, tcode)


def _normalize_diff(diff):
    if isinstance(diff, list):
        return diff
    if isinstance(diff, dict):
        return list(diff.values())
    return []


def _board_for(code):
    code = str(code or "")
    if code.startswith(("300", "301")):
        return "chinext"
    if code.startswith(("688", "689")):
        return "star_market"
    if code.startswith(("4", "8", "92")):
        return "bse"
    return "main_board"


def _exchange_for(code):
    code = str(code or "")
    if code.startswith(("4", "8", "92")):
        return "BJ"
    if code.startswith(("6", "68", "69")):
        return "SH"
    if code.startswith(("0", "3")):
        return "SZ"
    return "UNKNOWN"


def _limit_threshold_percent(code, name):
    name_upper = str(name or "").upper()
    if "ST" in name_upper:
        return 5.0
    board = _board_for(code)
    if board in {"chinext", "star_market"}:
        return 20.0
    if board == "bse":
        return 30.0
    return 10.0


def _record_metrics(record):
    code = str(record.get("f12") or "")
    name = str(record.get("f14") or code)
    pct = _as_float(record.get("f3"))
    amount = _as_float(record.get("f6"))
    high = _as_float(record.get("f15"))
    previous_close = _as_float(record.get("f18"))
    threshold = _limit_threshold_percent(code, name)
    high_pct = None
    if high is not None and previous_close not in (None, 0):
        high_pct = (high / previous_close - 1.0) * 100.0

    near = 0.18
    is_limit_up = pct is not None and pct >= threshold - near
    is_limit_down = pct is not None and pct <= -threshold + near
    touched_limit_up = high_pct is not None and high_pct >= threshold - near
    broken_limit_up = bool(touched_limit_up and not is_limit_up and pct is not None and pct < threshold - 0.5)

    return {
        "code": code,
        "name": name,
        "board": _board_for(code),
        "exchange": _exchange_for(code),
        "change_percent": pct,
        "amount_raw": amount,
        "is_limit_up_approx": is_limit_up,
        "is_limit_down_approx": is_limit_down,
        "broken_limit_up_approx": broken_limit_up,
    }


def _summarize_market_records(records):
    metrics = [_record_metrics(x) for x in records]
    usable = [x for x in metrics if x["change_percent"] is not None]
    pcts = [x["change_percent"] for x in usable]
    up = sum(1 for x in pcts if x > 0)
    down = sum(1 for x in pcts if x < 0)
    flat = len(pcts) - up - down
    unavailable = len(metrics) - len(usable)
    amount_raw = sum(x["amount_raw"] or 0.0 for x in metrics)

    return {
        "count": len(metrics),
        "change_covered_count": len(usable),
        "unavailable_change_count": unavailable,
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "up_ratio_percent": _round(up / len(pcts) * 100.0, 2) if pcts else None,
        "down_ratio_percent": _round(down / len(pcts) * 100.0, 2) if pcts else None,
        "breadth_score_percent": _round((up - down) / len(pcts) * 100.0, 2) if pcts else None,
        "mean_change_percent": _round(_mean(pcts), 4),
        "median_change_percent": _round(_median(pcts), 4),
        "amount_1e8": _round(amount_raw / 1e8, 2),
        "move_ge_3pct_count": sum(1 for x in pcts if x >= 3.0),
        "move_le_minus_3pct_count": sum(1 for x in pcts if x <= -3.0),
        "limit_up_count_approx": sum(1 for x in metrics if x["is_limit_up_approx"]),
        "limit_down_count_approx": sum(1 for x in metrics if x["is_limit_down_approx"]),
        "broken_limit_up_count_approx": sum(1 for x in metrics if x["broken_limit_up_approx"]),
    }


def fetch_market_breadth(base, now, indices=None):
    raise RuntimeError("market breadth source is not configured")


def install(base):
    global LAST_BREADTH
    original_fetch_indices = base.fetch_indices

    def fetch_indices_with_breadth(now):
        global LAST_BREADTH
        indices = original_fetch_indices(now)
        try:
            LAST_BREADTH = fetch_market_breadth(base, now, indices)
            overall = LAST_BREADTH.get("overall") or {}
            print(
                "MARKET_BREADTH "
                f"status={LAST_BREADTH.get('status')} "
                f"coverage={LAST_BREADTH.get('covered_count')}/{LAST_BREADTH.get('reported_total_count')} "
                f"up/down/flat/unavailable={overall.get('up_count')}/{overall.get('down_count')}/"
                f"{overall.get('flat_count')}/{overall.get('unavailable_change_count')} "
                f"session={LAST_BREADTH.get('market_session_date')} freshness={LAST_BREADTH.get('freshness')} "
                f"amount_1e8={overall.get('amount_1e8')}",
                flush=True,
            )
        except Exception as exc:
            LAST_BREADTH = {
                "status": "ERROR",
                "source": "market breadth source",
                "collected_at_cst": now.strftime("%Y-%m-%d %H:%M:%S"),
                "market_session_date": None,
                "freshness": "UNKNOWN",
                "freshness_basis": "SOURCE_FAILURE",
                "error": f"{type(exc).__name__}: {exc}",
            }
            print(f"MARKET_BREADTH status=ERROR error={LAST_BREADTH['error']}", flush=True)
        return indices

    base.fetch_indices = fetch_indices_with_breadth


def _index_quote(item):
    item = item or {}
    quote = item.get("quote") or {}
    pct = _as_float(quote.get("change_percent"))
    freshness = quote.get("freshness")
    if pct is None or freshness not in USABLE_FRESHNESS:
        return None
    return quote


def _bias(mean_pct, up_count, down_count, total_count, dispersion_pct=None):
    if mean_pct is None or total_count <= 0:
        return "UNKNOWN"
    up_ratio = up_count / total_count
    down_ratio = down_count / total_count
    if mean_pct >= 1.0 and up_ratio >= 2 / 3:
        return "STRONG_BULLISH"
    if mean_pct <= -1.0 and down_ratio >= 2 / 3:
        return "STRONG_BEARISH"
    if mean_pct >= 0.3 and up_count > down_count:
        return "BULLISH"
    if mean_pct <= -0.3 and down_count > up_count:
        return "BEARISH"
    if up_count and down_count and (dispersion_pct or 0) >= 1.0:
        return "MIXED"
    return "NEUTRAL"


def _build_indices(indices):
    members = []
    values = []
    by_name = {}
    for name in CORE_INDICES:
        item = (indices or {}).get(name) or {}
        quote = _index_quote(item)
        pct = _as_float((quote or {}).get("change_percent"))
        member = {
            "name": name,
            "status": item.get("status") or "MISSING",
            "source": (quote or {}).get("source"),
            "latest": (quote or {}).get("latest"),
            "change_percent": pct,
            "freshness": (quote or {}).get("freshness"),
            "market_time_cst": (quote or {}).get("market_time_cst"),
            "available": quote is not None,
        }
        members.append(member)
        by_name[name] = member
        if pct is not None:
            values.append(pct)

    broad_names = ["上证指数", "沪深300", "深证成指"]
    broad_values = [
        _as_float((by_name.get(name) or {}).get("change_percent"))
        for name in broad_names
    ]
    broad_covered = sum(value is not None for value in broad_values)
    broad_reference = _mean(broad_values)
    if broad_covered == len(broad_names):
        broad_quality = "HIGH"
    elif broad_covered >= 2:
        broad_quality = "MEDIUM"
    else:
        broad_quality = "LOW"

    up = sum(1 for x in values if x > 0)
    down = sum(1 for x in values if x < 0)
    flat = len(values) - up - down
    mean_pct = _mean(values)
    median_pct = _median(values)
    dispersion = max(values) - min(values) if len(values) >= 2 else 0.0 if values else None
    coverage = len(values) / len(CORE_INDICES) * 100.0

    return {
        "status": "OK" if len(values) == len(CORE_INDICES) else ("PARTIAL" if values else "ERROR"),
        "expected_count": len(CORE_INDICES),
        "covered_count": len(values),
        "coverage_percent": _round(coverage, 2),
        "mean_change_percent": _round(mean_pct, 4),
        "median_change_percent": _round(median_pct, 4),
        "broad_market_reference_percent": _round(broad_reference, 4),
        "broad_market_reference_members": broad_names,
        "broad_market_reference_expected_count": len(broad_names),
        "broad_market_reference_covered_count": broad_covered,
        "broad_market_reference_quality": broad_quality,
        "dispersion_percent": _round(dispersion, 4),
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "breadth_score_percent": _round((up - down) / len(values) * 100.0, 2) if values else None,
        "bias": _bias(mean_pct, up, down, len(values), dispersion),
        "members": members,
    }


def _build_style(index_summary):
    by_name = {item["name"]: item for item in index_summary.get("members", [])}

    def pct(name):
        return _as_float((by_name.get(name) or {}).get("change_percent"))

    hs300 = pct("沪深300")
    csi1000 = pct("中证1000")
    growth_values = [pct("创业板指"), pct("科创50")]
    growth_covered = sum(value is not None for value in growth_values)
    growth = _mean(growth_values)
    growth_quality = "HIGH" if growth_covered == 2 else ("MEDIUM" if growth_covered == 1 else "LOW")
    sh = pct("上证指数")
    sz = pct("深证成指")
    small_vs_large = csi1000 - hs300 if csi1000 is not None and hs300 is not None else None
    growth_vs_large = growth - hs300 if growth is not None and hs300 is not None else None
    sz_vs_sh = sz - sh if sz is not None and sh is not None else None

    if small_vs_large is None and growth_vs_large is None:
        dominant = "UNKNOWN"
    elif (small_vs_large or 0) >= 0.5 and (growth_vs_large or 0) >= 0.5:
        dominant = "SMALL_GROWTH_LEADING"
    elif (small_vs_large or 0) <= -0.5 and (growth_vs_large or 0) <= -0.5:
        dominant = "LARGE_VALUE_LEADING"
    elif small_vs_large is not None and small_vs_large >= 0.5:
        dominant = "SMALL_CAP_LEADING"
    elif small_vs_large is not None and small_vs_large <= -0.5:
        dominant = "LARGE_CAP_LEADING"
    elif growth_vs_large is not None and growth_vs_large >= 0.5:
        dominant = "GROWTH_LEADING"
    elif growth_vs_large is not None and growth_vs_large <= -0.5:
        dominant = "GROWTH_LAGGING"
    else:
        dominant = "BALANCED"

    return {
        "status": dominant,
        "references": {
            "large_cap_hs300_percent": _round(hs300, 4),
            "small_cap_csi1000_percent": _round(csi1000, 4),
            "growth_proxy_percent": _round(growth, 4),
            "growth_proxy_covered_count": growth_covered,
            "growth_proxy_expected_count": 2,
            "growth_proxy_quality": growth_quality,
            "shanghai_percent": _round(sh, 4),
            "shenzhen_percent": _round(sz, 4),
        },
        "spreads": {
            "small_vs_large_percent": _round(small_vs_large, 4),
            "growth_vs_large_percent": _round(growth_vs_large, 4),
            "shenzhen_vs_shanghai_percent": _round(sz_vs_sh, 4),
        },
    }


def _sector_reference_quality(group):
    group = group or {}
    members = group.get("members") or []
    requested = _as_int(group.get("requested_member_count"), len(members))
    covered = _as_int(
        group.get("covered_member_count"),
        sum(1 for member in members if member.get("available")),
    )
    coverage = _as_float(group.get("coverage_percent"))
    if coverage is None:
        coverage = covered / requested * 100.0 if requested else 0.0
    status = group.get("status") or "UNKNOWN"
    available = _as_float(group.get("mean_change_percent")) is not None

    if available and covered >= 5 and coverage >= 75.0 and status == "OK":
        quality = "HIGH"
    elif available and covered >= 3 and coverage >= 50.0 and status in {"OK", "PARTIAL"}:
        quality = "MEDIUM"
    else:
        quality = "LOW"

    return {
        "available": available,
        "quality": quality,
        "status": status,
        "requested_member_count": requested,
        "covered_member_count": covered,
        "coverage_percent": _round(coverage, 2),
        "high_confidence_rule": "covered>=5 && coverage>=75% && status=OK",
    }


def _build_groups(groups):
    out = {}
    for group_id, group in (groups or {}).items():
        mean_pct = _as_float(group.get("mean_change_percent"))
        breadth = _as_float(group.get("breadth_score_percent"))
        up = _as_int(group.get("up_count"))
        down = _as_int(group.get("down_count"))
        flat = _as_int(group.get("flat_count"))
        total = up + down + flat
        out[group_id] = {
            "label": group.get("label") or group_id,
            "status": group.get("status") or "UNKNOWN",
            "requested_member_count": _as_int(group.get("requested_member_count")),
            "active_member_count": _as_int(group.get("active_member_count")),
            "covered_member_count": _as_int(group.get("covered_member_count")),
            "mean_change_percent": _round(mean_pct, 4),
            "median_change_percent": _round(_as_float(group.get("median_change_percent")), 4),
            "breadth_score_percent": _round(breadth, 2),
            "coverage_percent": _round(_as_float(group.get("coverage_percent")), 2),
            "reference_quality": _sector_reference_quality(group),
            "bias": _bias(mean_pct, up, down, total),
            "target": group.get("target"),
            "target_vs_peer_mean_percent": _round(_as_float(group.get("target_vs_peer_mean_percent")), 4),
        }
    return out


def _market_regime(index_summary, breadth):
    market_reference = _as_float(index_summary.get("broad_market_reference_percent"))
    overall = (breadth or {}).get("overall") or {}
    up_ratio = _as_float(overall.get("up_ratio_percent"))
    down_ratio = _as_float(overall.get("down_ratio_percent"))
    score = _as_float(overall.get("breadth_score_percent"))

    if market_reference is None:
        return {"status": "UNKNOWN", "reason_codes": ["BROAD_INDEX_DATA_MISSING"]}
    if up_ratio is None or down_ratio is None:
        return {
            "status": "UNKNOWN",
            "reason_codes": ["MARKET_BREADTH_MISSING"],
            "broad_market_reference_percent": _round(market_reference, 4),
        }

    if market_reference >= 0.5 and up_ratio >= 60:
        status = "BROAD_RISK_ON"
    elif market_reference <= -0.5 and down_ratio >= 60:
        status = "BROAD_RISK_OFF"
    elif market_reference >= 0.5 and up_ratio < 50:
        status = "INDEX_UP_NARROW"
    elif market_reference <= -0.5 and down_ratio < 50:
        status = "INDEX_DOWN_BREADTH_RESILIENT"
    elif abs(market_reference) <= 0.35 and abs(score or 0) <= 15:
        status = "BALANCED"
    else:
        status = "ROTATION_MIXED"

    return {
        "status": status,
        "broad_market_reference_percent": _round(market_reference, 4),
        "market_up_ratio_percent": _round(up_ratio, 2),
        "market_down_ratio_percent": _round(down_ratio, 2),
        "market_breadth_score_percent": _round(score, 2),
        "breadth_estimated": bool((breadth or {}).get("estimated")),
        "reason_codes": [status],
    }


def _relative_label(delta, tolerance=0.5):
    if delta is None:
        return "UNKNOWN"
    if delta >= tolerance:
        return "OUTPERFORM"
    if delta <= -tolerance:
        return "UNDERPERFORM"
    return "INLINE"


def _same_direction(a, b):
    if a is None or b is None or a == 0 or b == 0:
        return None
    return (a > 0) == (b > 0)


def _classification_separation(driver, vs_market, vs_sector, vs_style, sector_vs_market):
    market_distance = abs(vs_market) if vs_market is not None else None
    sector_distance = abs(vs_sector) if vs_sector is not None else None
    style_distance = abs(vs_style) if vs_style is not None else None
    sector_advantage = (
        market_distance - sector_distance
        if market_distance is not None and sector_distance is not None
        else None
    )
    style_advantage = (
        market_distance - style_distance
        if market_distance is not None and style_distance is not None
        else None
    )

    if driver == "SECTOR":
        if sector_distance is not None and sector_advantage is not None and sector_distance <= 0.35 and sector_advantage >= 0.5:
            quality = "HIGH"
        elif sector_distance is not None and sector_advantage is not None and sector_distance <= 0.6 and sector_advantage >= 0.25:
            quality = "MEDIUM"
        else:
            quality = "LOW"
    elif driver == "MARKET":
        if market_distance is not None and market_distance <= 0.35 and (
            sector_distance is None
            or (sector_vs_market is not None and abs(sector_vs_market) <= 0.4)
            or (sector_distance - market_distance) >= 0.3
        ):
            quality = "HIGH"
        elif market_distance is not None and market_distance <= 0.6:
            quality = "MEDIUM"
        else:
            quality = "LOW"
    elif driver == "STYLE":
        if style_distance is not None and style_advantage is not None and style_distance <= 0.35 and style_advantage >= 0.5:
            quality = "HIGH"
        elif style_distance is not None and style_advantage is not None and style_distance <= 0.6 and style_advantage >= 0.25:
            quality = "MEDIUM"
        else:
            quality = "LOW"
    elif driver == "IDIOSYNCRATIC":
        if market_distance is not None and market_distance >= 1.2 and (sector_distance is None or sector_distance >= 1.2):
            quality = "HIGH"
        elif market_distance is not None and market_distance >= 0.9 and (sector_distance is None or sector_distance >= 0.9):
            quality = "MEDIUM"
        else:
            quality = "LOW"
    elif driver == "MIXED":
        quality = "MEDIUM"
    else:
        quality = "LOW"

    return {
        "quality": quality,
        "market_distance_percent": _round(market_distance, 4),
        "sector_distance_percent": _round(sector_distance, 4),
        "style_distance_percent": _round(style_distance, 4),
        "sector_advantage_over_market_percent": _round(sector_advantage, 4),
        "style_advantage_over_market_percent": _round(style_advantage, 4),
    }


def _driver_attribution(
    stock_pct,
    market_pct,
    sector_pct,
    style_pct=None,
    market_quality=None,
    sector_quality=None,
    style_quality=None,
):
    market_quality = market_quality or {"quality": "LOW", "available": False}
    sector_quality = sector_quality or {"quality": "LOW", "available": False}
    style_quality = style_quality or {"quality": "LOW", "available": False}

    if stock_pct is None:
        return {
            "primary_driver": "UNKNOWN",
            "confidence": "LOW",
            "market_reference_quality": market_quality,
            "sector_reference_quality": sector_quality,
            "style_reference_quality": style_quality,
            "reason_codes": ["STOCK_CHANGE_MISSING"],
        }

    vs_market = stock_pct - market_pct if market_pct is not None else None
    vs_sector = stock_pct - sector_pct if sector_pct is not None else None
    vs_style = stock_pct - style_pct if style_pct is not None else None
    sector_vs_market = sector_pct - market_pct if sector_pct is not None and market_pct is not None else None
    market_track = vs_market is not None and abs(vs_market) <= 0.6 and _same_direction(stock_pct, market_pct) is not False
    sector_track = vs_sector is not None and abs(vs_sector) <= 0.6 and _same_direction(stock_pct, sector_pct) is not False
    style_track = vs_style is not None and abs(vs_style) <= 0.6 and _same_direction(stock_pct, style_pct) is not False
    reasons = []

    if sector_track and sector_vs_market is not None and abs(sector_vs_market) >= 0.6:
        driver = "SECTOR"
        reasons.extend(["STOCK_TRACKS_SECTOR", "SECTOR_DIVERGES_FROM_MARKET"])
    elif style_track and not market_track:
        driver = "STYLE"
        reasons.extend(["STOCK_TRACKS_STYLE_PROXY", "STOCK_DIVERGES_FROM_BROAD_MARKET"])
    elif market_track and (sector_pct is None or sector_track or abs(sector_vs_market or 0) < 0.6):
        driver = "MARKET"
        reasons.append("STOCK_TRACKS_BROAD_MARKET")
        if sector_track:
            reasons.append("SECTOR_ALSO_TRACKS_MARKET")
    elif vs_market is not None and abs(vs_market) >= 0.9 and (vs_sector is None or abs(vs_sector) >= 0.9):
        driver = "IDIOSYNCRATIC"
        reasons.append("STOCK_DIVERGES_FROM_MARKET")
        if sector_pct is not None:
            reasons.append("STOCK_DIVERGES_FROM_SECTOR")
    else:
        driver = "MIXED"
        if vs_market is not None and abs(vs_market) >= 0.6:
            reasons.append("STOCK_DIVERGES_FROM_MARKET")
        if vs_sector is not None and abs(vs_sector) >= 0.6:
            reasons.append("STOCK_DIVERGES_FROM_SECTOR")
        if vs_sector is not None and vs_sector >= 0.6:
            reasons.append("STOCK_OUTPERFORMS_SECTOR")
        elif vs_sector is not None and vs_sector <= -0.6:
            reasons.append("STOCK_UNDERPERFORMS_SECTOR")
        if sector_vs_market is not None and abs(sector_vs_market) >= 0.6:
            reasons.append("SECTOR_DIVERGES_FROM_MARKET")
        if not reasons:
            reasons.append("MULTIPLE_DRIVERS_OR_WEAK_SEPARATION")

    separation = _classification_separation(driver, vs_market, vs_sector, vs_style, sector_vs_market)
    if driver == "SECTOR":
        confidence = _min_confidence(
            separation["quality"],
            market_quality.get("quality"),
            sector_quality.get("quality"),
        )
    elif driver == "MARKET":
        confidence = _min_confidence(separation["quality"], market_quality.get("quality"))
    elif driver == "STYLE":
        confidence = _min_confidence(
            separation["quality"],
            market_quality.get("quality"),
            style_quality.get("quality"),
        )
    elif driver == "IDIOSYNCRATIC":
        sector_cap = sector_quality.get("quality") if sector_pct is not None else "MEDIUM"
        confidence = _min_confidence(
            separation["quality"],
            market_quality.get("quality"),
            sector_cap,
        )
    elif driver == "MIXED":
        confidence = _min_confidence("MEDIUM", market_quality.get("quality"))
    else:
        confidence = "LOW"

    return {
        "primary_driver": driver,
        "confidence": confidence,
        "reference_availability": {
            "market": market_pct is not None,
            "sector": sector_pct is not None,
            "style": style_pct is not None,
        },
        "market_reference_quality": market_quality,
        "sector_reference_quality": sector_quality,
        "style_reference_quality": style_quality,
        "classification_separation": separation,
        "evidence": {
            "stock_change_percent": _round(stock_pct, 4),
            "market_reference_percent": _round(market_pct, 4),
            "sector_reference_percent": _round(sector_pct, 4),
            "style_reference_percent": _round(style_pct, 4),
            "stock_vs_market_percent": _round(vs_market, 4),
            "stock_vs_sector_percent": _round(vs_sector, 4),
            "stock_vs_style_percent": _round(vs_style, 4),
            "sector_vs_market_percent": _round(sector_vs_market, 4),
        },
        "reason_codes": reasons,
    }


def _market_reference_quality(index_summary):
    covered = _as_int(index_summary.get("broad_market_reference_covered_count"))
    expected = _as_int(index_summary.get("broad_market_reference_expected_count"), 3)
    quality = index_summary.get("broad_market_reference_quality") or "LOW"
    return {
        "available": _as_float(index_summary.get("broad_market_reference_percent")) is not None,
        "quality": quality,
        "covered_index_count": covered,
        "expected_index_count": expected,
        "coverage_percent": _round(covered / expected * 100.0, 2) if expected else None,
        "members": list(index_summary.get("broad_market_reference_members") or []),
    }


def _style_reference_for_code(code, style):
    code = str(code or "")
    refs = (style or {}).get("references") or {}
    if code.startswith(("300", "301", "688", "689")):
        value = _as_float(refs.get("growth_proxy_percent"))
        quality = refs.get("growth_proxy_quality") or "LOW"
        return (
            value,
            "GROWTH_PROXY",
            {
                "available": value is not None,
                "quality": quality,
                "reference_type": "GROWTH_PROXY",
                "covered_index_count": _as_int(refs.get("growth_proxy_covered_count")),
                "expected_index_count": _as_int(refs.get("growth_proxy_expected_count"), 2),
            },
        )
    return None, None, {"available": False, "quality": "LOW", "reference_type": None}


def _build_targets(detail_stocks, groups, index_summary, style):
    market_reference = _as_float(index_summary.get("broad_market_reference_percent"))
    market_quality = _market_reference_quality(index_summary)
    group_by_target = {}
    for group_id, group in (groups or {}).items():
        target = group.get("target") or {}
        code = target.get("code")
        if code and code not in group_by_target:
            group_by_target[code] = (group_id, group)

    out = {}
    for code, item in (detail_stocks or {}).items():
        quote = (item or {}).get("quote") or {}
        pct = _as_float(quote.get("change_percent"))
        vs_market = pct - market_reference if pct is not None and market_reference is not None else None
        group_id = None
        group_mean = None
        vs_group = None
        sector_quality = {"available": False, "quality": "LOW", "status": "NO_GROUP"}
        if code in group_by_target:
            group_id, group = group_by_target[code]
            group_mean = _as_float(group.get("mean_change_percent"))
            vs_group = pct - group_mean if pct is not None and group_mean is not None else None
            sector_quality = _sector_reference_quality(group)

        style_ref, style_ref_type, style_quality = _style_reference_for_code(code, style)
        intraday = (item or {}).get("intraday") or {}
        out[code] = {
            "name": quote.get("name"),
            "change_percent": _round(pct, 4),
            "freshness": quote.get("freshness"),
            "relative_strength": {
                "market_reference_percent": _round(market_reference, 4),
                "vs_market_percent": _round(vs_market, 4),
                "relative_to_market": _relative_label(vs_market),
                "group_id": group_id,
                "group_reference_percent": _round(group_mean, 4),
                "vs_group_mean_percent": _round(vs_group, 4),
                "relative_to_group": _relative_label(vs_group),
                "style_reference_type": style_ref_type,
                "style_reference_percent": _round(style_ref, 4),
                "vs_style_reference_percent": _round(pct - style_ref, 4) if pct is not None and style_ref is not None else None,
                "relative_to_style": _relative_label(pct - style_ref) if pct is not None and style_ref is not None else "UNKNOWN",
            },
            "driver_attribution": _driver_attribution(
                pct,
                market_reference,
                group_mean,
                style_ref,
                market_quality=market_quality,
                sector_quality=sector_quality,
                style_quality=style_quality,
            ),
            "intraday_context": {
                "bias": intraday.get("bias") or intraday.get("structure_bias"),
                "price_vs_vwap_percent": _round(_as_float(intraday.get("price_vs_vwap_percent")), 4),
            },
        }
    return out


def _confidence(index_summary, group_summary, breadth, snapshot):
    coverage = _as_float(index_summary.get("coverage_percent")) or 0.0
    breadth_status = (breadth or {}).get("status") or "ERROR"
    breadth_freshness = (breadth or {}).get("freshness") or "UNKNOWN"
    resilience = ((snapshot.get("quote_resilience") or {}).get("status") or "UNKNOWN").upper()
    guard = ((snapshot.get("live_price_guard") or {}).get("status") or "UNKNOWN").upper()
    group_errors = sum(1 for x in group_summary.values() if x.get("status") == "ERROR")

    if (
        coverage >= 99.9
        and breadth_status == "OK"
        and breadth_freshness in USABLE_FRESHNESS
        and resilience == "OK"
        and guard not in {"ERROR", "VIOLATION"}
        and group_errors == 0
    ):
        return "HIGH"
    if (
        coverage >= 66.0
        and breadth_status in {"OK", "PARTIAL"}
        and breadth_freshness in USABLE_FRESHNESS
        and guard not in {"ERROR", "VIOLATION"}
    ):
        return "MEDIUM"
    return "LOW"


def _fmt_pct(value):
    value = _as_float(value)
    if value is None:
        return "-"
    return f"{value:+.2f}%"


def _summary_text(index_summary, breadth, regime, style, groups, confidence):
    parts = [REGIME_ZH.get(regime.get("status"), "市场状态未知")]
    parts.append(
        "宽基：" + "、".join(
            f"{item['name']} {_fmt_pct(item.get('change_percent'))}"
            for item in index_summary.get("members", [])
            if item.get("available")
        )
    )
    overall = (breadth or {}).get("overall") or {}
    if overall:
        prefix = "估算" if overall.get("estimated") else ""
        text = (
            f"全市场{prefix}上涨/下跌/平盘 "
            f"{overall.get('up_count')}/{overall.get('down_count')}/{overall.get('flat_count')}"
        )
        unavailable = overall.get("unavailable_change_count")
        if unavailable:
            text += f"，另有约 {unavailable} 只无涨跌数据"
        amount = overall.get("amount_1e8")
        if amount is not None:
            text += f"，成交额约 {amount} 亿元"
        elif overall.get("sample_amount_1e8") is not None:
            text += "，全市场总成交额在样本模式下不外推"
        parts.append(text)
    parts.append(f"风格 {style.get('status')}")
    group_parts = []
    for group in groups.values():
        if group.get("mean_change_percent") is not None:
            group_parts.append(f"{group.get('label')} {_fmt_pct(group.get('mean_change_percent'))}")
    if group_parts:
        parts.append("板块：" + "、".join(group_parts))
    parts.append(f"数据置信度 {confidence}")
    return "；".join(parts) + "。"


def build_market_environment(snapshot, breadth=None):
    index_summary = _build_indices(snapshot.get("indices") or {})
    style = _build_style(index_summary)
    group_summary = _build_groups(snapshot.get("groups") or {})
    regime = _market_regime(index_summary, breadth)
    targets = _build_targets(snapshot.get("detail_stocks") or {}, snapshot.get("groups") or {}, index_summary, style)
    confidence = _confidence(index_summary, group_summary, breadth, snapshot)

    status = index_summary.get("status")
    breadth_status = (breadth or {}).get("status")
    if status == "OK" and breadth_status in {"PARTIAL", "ERROR", None}:
        status = "PARTIAL"
    if any(x.get("status") == "ERROR" for x in group_summary.values()) and status != "ERROR":
        status = "PARTIAL"

    return {
        "status": status,
        "confidence": confidence,
        "indices": index_summary,
        "breadth": breadth,
        "regime": regime,
        "style": style,
        "groups": group_summary,
        "targets": targets,
        "data_quality": {
            "quote_resilience_status": (snapshot.get("quote_resilience") or {}).get("status"),
            "live_price_guard_status": (snapshot.get("live_price_guard") or {}).get("status"),
            "market_window": snapshot.get("market_window"),
            "breadth_status": breadth_status,
            "breadth_estimated": bool((breadth or {}).get("estimated")),
            "breadth_freshness": (breadth or {}).get("freshness"),
            "breadth_market_session_date": (breadth or {}).get("market_session_date"),
            "breadth_freshness_basis": (breadth or {}).get("freshness_basis"),
            "breadth_sample_coverage_percent": ((breadth or {}).get("sampling") or {}).get("sample_coverage_percent"),
            "breadth_all_strata_covered": ((breadth or {}).get("sampling") or {}).get("all_strata_covered"),
            "breadth_bootstrap_state": (breadth or {}).get("bootstrap_state"),
            "breadth_bootstrap_key": (breadth or {}).get("bootstrap_key"),
            "breadth_session_segment": (breadth or {}).get("session_segment"),
            "breadth_bootstrap_at": (breadth or {}).get("bootstrap_at"),
            "breadth_cache_age_seconds": (breadth or {}).get("cache_age_seconds"),
            "breadth_source_session": (breadth or {}).get("source_session"),
            "breadth_fetched_at": (breadth or {}).get("fetched_at"),
            "breadth_freshness_status": (breadth or {}).get("freshness_status"),
            "breadth_bootstrap_revision": (breadth or {}).get("bootstrap_revision"),
            "breadth_quality": (breadth or {}).get("quality"),
        },
        "summary": _summary_text(index_summary, breadth, regime, style, group_summary, confidence),
    }


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["market_environment"] = build_market_environment(data, LAST_BREADTH)
    data["schema_version"] = max(int(data.get("schema_version") or 0), 22)
    data.setdefault("features", {})["market_environment"] = "v1"
    data.setdefault("features", {})["breadth_bootstrap"] = "v1"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = data["market_environment"]
    breadth = result.get("breadth") or {}
    print(
        "MARKET_ENVIRONMENT "
        f"status={result['status']} confidence={result['confidence']} regime={result['regime']['status']} "
        f"index_coverage={result['indices']['covered_count']}/{result['indices']['expected_count']} "
        f"breadth={breadth.get('status')} estimated={breadth.get('estimated')} "
        f"session={breadth.get('market_session_date')} freshness={breadth.get('freshness')} "
        f"bootstrap={breadth.get('bootstrap_state')} segment={breadth.get('session_segment')} "
        f"style={result['style']['status']}",
        flush=True,
    )
    print(
        "SNAPSHOT_SCHEMA_UPGRADED schema_version=22 "
        "features=market_environment:v1,breadth_bootstrap:v1",
        flush=True,
    )
