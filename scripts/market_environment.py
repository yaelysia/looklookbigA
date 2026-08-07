import json
import statistics
from pathlib import Path


EXPECTED_INDICES = ("上证指数", "深证成指", "创业板指")
USABLE_FRESHNESS = {"LIVE", "CURRENT_SESSION", "LAST_SESSION"}

BIAS_ZH = {
    "STRONG_BULLISH": "明显偏强",
    "BULLISH": "偏强",
    "NEUTRAL": "中性",
    "MIXED": "分化",
    "BEARISH": "偏弱",
    "STRONG_BEARISH": "明显偏弱",
    "UNKNOWN": "未知",
}

STYLE_ZH = {
    "GROWTH_LEADING": "成长风格领先",
    "GROWTH_LAGGING": "大盘/价值风格相对领先",
    "BALANCED": "风格相对均衡",
    "UNKNOWN": "风格信号不足",
}


def _as_float(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value, digits=4):
    return None if value is None else round(float(value), digits)


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
    for name in EXPECTED_INDICES:
        item = (indices or {}).get(name) or {}
        quote = _index_quote(item)
        pct = _as_float((quote or {}).get("change_percent"))
        member = {
            "name": name,
            "status": item.get("status") or "MISSING",
            "latest": (quote or {}).get("latest"),
            "change_percent": pct,
            "freshness": (quote or {}).get("freshness"),
            "market_time_cst": (quote or {}).get("market_time_cst"),
            "available": quote is not None,
        }
        members.append(member)
        if pct is not None:
            values.append(pct)

    up = sum(1 for x in values if x > 0)
    down = sum(1 for x in values if x < 0)
    flat = len(values) - up - down
    mean_pct = statistics.fmean(values) if values else None
    median_pct = statistics.median(values) if values else None
    dispersion = max(values) - min(values) if len(values) >= 2 else 0.0 if values else None
    coverage = len(values) / len(EXPECTED_INDICES) * 100.0

    return {
        "status": "OK" if len(values) == len(EXPECTED_INDICES) else ("PARTIAL" if values else "ERROR"),
        "expected_count": len(EXPECTED_INDICES),
        "covered_count": len(values),
        "coverage_percent": _round(coverage, 2),
        "mean_change_percent": _round(mean_pct, 4),
        "median_change_percent": _round(median_pct, 4),
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
    sh = _as_float((by_name.get("上证指数") or {}).get("change_percent"))
    growth_values = [
        _as_float((by_name.get("深证成指") or {}).get("change_percent")),
        _as_float((by_name.get("创业板指") or {}).get("change_percent")),
    ]
    growth_values = [x for x in growth_values if x is not None]

    if sh is None or not growth_values:
        return {
            "status": "UNKNOWN",
            "growth_proxy_change_percent": None,
            "shanghai_change_percent": sh,
            "growth_vs_shanghai_percent": None,
        }

    growth = statistics.fmean(growth_values)
    spread = growth - sh
    if spread >= 0.5:
        status = "GROWTH_LEADING"
    elif spread <= -0.5:
        status = "GROWTH_LAGGING"
    else:
        status = "BALANCED"

    return {
        "status": status,
        "growth_proxy_change_percent": _round(growth, 4),
        "shanghai_change_percent": _round(sh, 4),
        "growth_vs_shanghai_percent": _round(spread, 4),
    }


def _build_groups(groups):
    out = {}
    for group_id, group in (groups or {}).items():
        mean_pct = _as_float(group.get("mean_change_percent"))
        breadth = _as_float(group.get("breadth_score_percent"))
        up = int(group.get("up_count") or 0)
        down = int(group.get("down_count") or 0)
        flat = int(group.get("flat_count") or 0)
        total = up + down + flat
        out[group_id] = {
            "label": group.get("label") or group_id,
            "status": group.get("status") or "UNKNOWN",
            "mean_change_percent": _round(mean_pct, 4),
            "median_change_percent": _round(_as_float(group.get("median_change_percent")), 4),
            "breadth_score_percent": _round(breadth, 2),
            "coverage_percent": _round(_as_float(group.get("coverage_percent")), 2),
            "bias": _bias(mean_pct, up, down, total),
            "target": group.get("target"),
            "target_vs_peer_mean_percent": _round(_as_float(group.get("target_vs_peer_mean_percent")), 4),
        }
    return out


def _relative_label(delta):
    if delta is None:
        return "UNKNOWN"
    if delta >= 0.5:
        return "OUTPERFORM"
    if delta <= -0.5:
        return "UNDERPERFORM"
    return "INLINE"


def _build_targets(detail_stocks, groups, index_mean):
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
        vs_index = pct - index_mean if pct is not None and index_mean is not None else None
        group_id = None
        vs_group = None
        if code in group_by_target:
            group_id, group = group_by_target[code]
            vs_group = _as_float(group.get("target_vs_peer_mean_percent"))

        intraday = (item or {}).get("intraday") or {}
        out[code] = {
            "name": quote.get("name"),
            "change_percent": _round(pct, 4),
            "freshness": quote.get("freshness"),
            "vs_index_mean_percent": _round(vs_index, 4),
            "relative_to_market": _relative_label(vs_index),
            "group_id": group_id,
            "vs_group_mean_percent": _round(vs_group, 4),
            "relative_to_group": _relative_label(vs_group),
            "intraday_bias": intraday.get("bias") or intraday.get("structure_bias"),
            "price_vs_vwap_percent": _round(_as_float(intraday.get("price_vs_vwap_percent")), 4),
        }
    return out


def _confidence(index_summary, group_summary, snapshot):
    coverage = _as_float(index_summary.get("coverage_percent")) or 0.0
    resilience = ((snapshot.get("quote_resilience") or {}).get("status") or "UNKNOWN").upper()
    guard = ((snapshot.get("live_price_guard") or {}).get("status") or "UNKNOWN").upper()
    group_errors = sum(1 for x in group_summary.values() if x.get("status") == "ERROR")

    if coverage >= 99.9 and resilience not in {"WARNING", "ERROR"} and guard not in {"ERROR", "VIOLATION"} and group_errors == 0:
        return "HIGH"
    if coverage >= 66.0:
        return "MEDIUM"
    return "LOW"


def _fmt_pct(value):
    value = _as_float(value)
    if value is None:
        return "-"
    return f"{value:+.2f}%"


def _summary_text(index_summary, style, groups, confidence):
    member_text = "、".join(
        f"{item['name']} {_fmt_pct(item.get('change_percent'))}"
        for item in index_summary.get("members", [])
        if item.get("available")
    ) or "指数数据不足"

    parts = [f"市场{BIAS_ZH.get(index_summary.get('bias'), '未知')}：{member_text}"]
    parts.append(STYLE_ZH.get(style.get("status"), "风格信号不足"))

    group_parts = []
    for group in groups.values():
        if group.get("mean_change_percent") is None:
            continue
        group_parts.append(
            f"{group.get('label')} {BIAS_ZH.get(group.get('bias'), '未知')}"
            f"（均值{_fmt_pct(group.get('mean_change_percent'))}，广度{_fmt_pct(group.get('breadth_score_percent'))}）"
        )
    if group_parts:
        parts.append("；".join(group_parts))

    parts.append(f"数据置信度 {confidence}")
    return "；".join(parts) + "。"


def build_market_environment(snapshot):
    index_summary = _build_indices(snapshot.get("indices") or {})
    style = _build_style(index_summary)
    group_summary = _build_groups(snapshot.get("groups") or {})
    index_mean = _as_float(index_summary.get("mean_change_percent"))
    targets = _build_targets(snapshot.get("detail_stocks") or {}, snapshot.get("groups") or {}, index_mean)
    confidence = _confidence(index_summary, group_summary, snapshot)

    status = index_summary.get("status")
    if status == "OK" and any(x.get("status") == "ERROR" for x in group_summary.values()):
        status = "PARTIAL"

    return {
        "status": status,
        "market_bias": index_summary.get("bias"),
        "confidence": confidence,
        "indices": index_summary,
        "style": style,
        "groups": group_summary,
        "targets": targets,
        "data_quality": {
            "quote_resilience_status": (snapshot.get("quote_resilience") or {}).get("status"),
            "live_price_guard_status": (snapshot.get("live_price_guard") or {}).get("status"),
            "market_window": snapshot.get("market_window"),
        },
        "summary": _summary_text(index_summary, style, group_summary, confidence),
    }


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["market_environment"] = build_market_environment(data)
    data["schema_version"] = max(int(data.get("schema_version") or 0), 9)
    data.setdefault("features", {})["market_environment"] = "v1"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    env = data["market_environment"]
    print(
        "MARKET_ENVIRONMENT "
        f"status={env['status']} bias={env['market_bias']} confidence={env['confidence']} "
        f"index_coverage={env['indices']['covered_count']}/{env['indices']['expected_count']} "
        f"style={env['style']['status']}",
        flush=True,
    )
    print("SNAPSHOT_SCHEMA_UPGRADED schema_version=9 feature=market_environment:v1", flush=True)
