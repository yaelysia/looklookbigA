import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import history_store


CST = timezone(timedelta(hours=8))
SEVERITY_ORDER = {"NONE": 0, "MINOR": 1, "MODERATE": 2, "SIGNIFICANT": 3}

THRESHOLDS = {
    "stock_change_percent_delta_abs": {
        "MINOR": 0.25,
        "MODERATE": 0.75,
        "SIGNIFICANT": 1.50,
    },
    "stock_price_delta_percent_abs": {
        "MINOR": 0.30,
        "MODERATE": 0.80,
        "SIGNIFICANT": 1.50,
    },
    "turnover_delta_percent_abs": {
        "MINOR": 15.0,
        "MODERATE": 40.0,
        "SIGNIFICANT": 100.0,
    },
    "intraday_metric_delta_abs": {
        "MINOR": 0.25,
        "MODERATE": 0.75,
        "SIGNIFICANT": 1.50,
    },
    "group_rank_change_abs": {
        "MINOR": 1,
        "MODERATE": 2,
        "SIGNIFICANT": 4,
    },
    "group_breadth_delta_abs": {
        "MINOR": 10.0,
        "MODERATE": 25.0,
        "SIGNIFICANT": 50.0,
    },
    "index_change_delta_abs": {
        "MINOR": 0.20,
        "MODERATE": 0.60,
        "SIGNIFICANT": 1.20,
    },
    "market_breadth_delta_abs": {
        "MINOR": 10.0,
        "MODERATE": 25.0,
        "SIGNIFICANT": 50.0,
    },
    "relative_strength_delta_abs": {
        "MINOR": 0.25,
        "MODERATE": 0.60,
        "SIGNIFICANT": 1.20,
    },
    "long_baseline_interval_seconds": 6 * 60 * 60,
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


def _parse_time(value):
    if not value:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=CST)
        except ValueError:
            continue
    return None


def _snapshot_time(snapshot):
    return _parse_time(snapshot.get("runner_time_utc")) or _parse_time(snapshot.get("runner_time_cst"))


def _market_date(item):
    if not isinstance(item, dict):
        return None
    quote = item.get("quote") if "quote" in item else item
    if not isinstance(quote, dict):
        return None
    value = quote.get("market_time_cst")
    dt = _parse_time(value)
    return dt.strftime("%Y-%m-%d") if dt else None


def _numeric_change(before, after, digits=4):
    b = _as_float(before)
    a = _as_float(after)
    delta = a - b if a is not None and b is not None else None
    relative = None
    if delta is not None and b not in (None, 0):
        relative = delta / abs(b) * 100.0
    return {
        "before": _round(b, digits),
        "after": _round(a, digits),
        "delta": _round(delta, digits),
        "delta_percent_of_before": _round(relative, 3),
        "comparable": delta is not None,
    }


def _state_change(before, after):
    comparable = before is not None and after is not None
    return {
        "before": before,
        "after": after,
        "changed": bool(comparable and before != after),
        "comparable": comparable,
    }


def _severity_for(value, threshold_key):
    value = _as_float(value)
    if value is None:
        return "NONE"
    magnitude = abs(value)
    thresholds = THRESHOLDS[threshold_key]
    if magnitude >= thresholds["SIGNIFICANT"]:
        return "SIGNIFICANT"
    if magnitude >= thresholds["MODERATE"]:
        return "MODERATE"
    if magnitude >= thresholds["MINOR"]:
        return "MINOR"
    return "NONE"


def _max_severity(*values):
    best = "NONE"
    for value in values:
        if SEVERITY_ORDER.get(value, 0) > SEVERITY_ORDER[best]:
            best = value
    return best


def _add_reason(reasons, severity, reason, value=None):
    if severity == "NONE":
        return
    entry = {"severity": severity, "reason": reason}
    if value is not None:
        entry["value"] = _round(value, 4) if isinstance(value, (int, float)) else value
    reasons.append(entry)


def _baseline_descriptor(previous, current, previous_path):
    prev_time = _snapshot_time(previous)
    current_time = _snapshot_time(current)
    interval = None
    if prev_time and current_time:
        interval = (current_time - prev_time).total_seconds()
    schema = int(previous.get("schema_version") or 0)
    flags = []
    if schema < 9 or not isinstance(previous.get("market_environment"), dict):
        flags.append("BASELINE_MISSING_MARKET_ENVIRONMENT")
    if schema < 10 or not isinstance(previous.get("data_quality"), dict):
        flags.append("BASELINE_PREDATES_DATA_PROVENANCE")
    if not isinstance(previous.get("observation"), dict):
        flags.append("BASELINE_PREDATES_OBSERVATION_IDENTITY")
    if interval is None or interval <= 0:
        flags.append("INVALID_BASELINE_INTERVAL")
    elif interval > THRESHOLDS["long_baseline_interval_seconds"]:
        flags.append("LONG_BASELINE_INTERVAL")

    quality = "OK" if not flags else "PARTIAL"
    previous_observation = previous.get("observation") or {}
    current_observation = current.get("observation") or {}
    return {
        "previous_snapshot_path": previous_path,
        "previous_snapshot_time": prev_time.isoformat(timespec="seconds") if prev_time else previous.get("runner_time_cst"),
        "current_snapshot_time": current_time.isoformat(timespec="seconds") if current_time else current.get("runner_time_cst"),
        "interval_seconds": _round(interval, 3),
        "previous_schema_version": schema,
        "previous_overall_data_quality": ((previous.get("data_quality") or {}).get("overall")),
        "current_overall_data_quality": ((current.get("data_quality") or {}).get("overall")),
        "previous_observation": {
            "run_id": previous_observation.get("run_id"),
            "run_attempt": previous_observation.get("run_attempt"),
            "head_sha": previous_observation.get("head_sha"),
        },
        "current_observation": {
            "run_id": current_observation.get("run_id"),
            "run_attempt": current_observation.get("run_attempt"),
            "head_sha": current_observation.get("head_sha"),
        },
        "quality": quality,
        "quality_flags": flags,
    }


def _stock_relative(snapshot, code):
    target = (((snapshot.get("market_environment") or {}).get("targets") or {}).get(code) or {})
    return target.get("relative_strength") or {}, target.get("driver_attribution") or {}


def _window_benchmarks(item):
    context = (item or {}).get("relative_strength_windows") or {}
    result = {}
    for kind, container in (
        ("GROUP", context.get("vs_groups") or {}),
        ("INDEX", context.get("vs_indices") or {}),
    ):
        for benchmark_id, benchmark in container.items():
            result[(kind, benchmark_id)] = benchmark or {}
    return context, result


def _relative_window_changes(before_item, after_item):
    before_context, before_benchmarks = _window_benchmarks(before_item)
    after_context, after_benchmarks = _window_benchmarks(after_item)
    same_session = bool(
        before_context.get("session_date")
        and before_context.get("session_date") == after_context.get("session_date")
    )
    result = {"same_session": same_session, "vs_groups": {}, "vs_indices": {}}
    for benchmark_key in sorted(set(before_benchmarks) | set(after_benchmarks)):
        kind, benchmark_id = benchmark_key
        before = before_benchmarks.get(benchmark_key) or {}
        after = after_benchmarks.get(benchmark_key) or {}
        windows = {}
        for window_id in sorted(
            set((before.get("windows") or {})) | set((after.get("windows") or {}))
        ):
            before_window = (before.get("windows") or {}).get(window_id) or {}
            after_window = (after.get("windows") or {}).get(window_id) or {}
            before_coverage = before_window.get("coverage") or {}
            after_coverage = after_window.get("coverage") or {}
            universe_comparable = True
            quality_flags = []
            if not same_session:
                universe_comparable = False
                quality_flags.append("RELATIVE_WINDOW_SESSION_CHANGED")
            if kind == "GROUP":
                requested_before = sorted(set(before_coverage.get("requested_peer_codes") or []))
                requested_after = sorted(set(after_coverage.get("requested_peer_codes") or []))
                covered_before = sorted(set(before_coverage.get("covered_peer_codes") or []))
                covered_after = sorted(set(after_coverage.get("covered_peer_codes") or []))
                if requested_before != requested_after:
                    universe_comparable = False
                    quality_flags.append("REQUESTED_PEER_UNIVERSE_CHANGED")
                if covered_before != covered_after:
                    universe_comparable = False
                    quality_flags.append("COVERED_PEER_UNIVERSE_CHANGED")
                if before_coverage.get("aggregation_method") != after_coverage.get(
                    "aggregation_method"
                ):
                    universe_comparable = False
                    quality_flags.append("PEER_AGGREGATION_METHOD_CHANGED")
                coverage_change = _numeric_change(
                    before_coverage.get("peer_coverage_percent"),
                    after_coverage.get("peer_coverage_percent"),
                )
            else:
                coverage_change = None
                if before.get("tcode") != after.get("tcode") or before.get("name") != after.get("name"):
                    universe_comparable = False
                    quality_flags.append("INDEX_BENCHMARK_IDENTITY_CHANGED")

            excess_change = _numeric_change(
                before_window.get("excess_return_percent"),
                after_window.get("excess_return_percent"),
            )
            excess_comparable = bool(excess_change.get("comparable"))
            if not universe_comparable or not excess_comparable:
                excess_change = {
                    "before": excess_change.get("before"),
                    "after": excess_change.get("after"),
                    "delta": None,
                    "delta_percent_of_before": None,
                    "comparable": False,
                }
            state_change = _state_change(
                before_window.get("state"), after_window.get("state")
            )
            if not universe_comparable or not excess_comparable:
                state_change["changed"] = False
                state_change["comparable"] = False
            windows[window_id] = {
                "window_before": {
                    "start": before_window.get("window_start"),
                    "end": before_window.get("window_end"),
                },
                "window_after": {
                    "start": after_window.get("window_start"),
                    "end": after_window.get("window_end"),
                },
                "excess_return_percent": excess_change,
                "state": state_change,
                "coverage_percent": coverage_change,
                "benchmark_universe_comparable": universe_comparable,
                "quality_flags": quality_flags,
            }
        target = result["vs_groups"] if kind == "GROUP" else result["vs_indices"]
        target[benchmark_id] = {
            "name": after.get("name") or before.get("name"),
            "tcode": after.get("tcode") or before.get("tcode"),
            "windows": windows,
        }
    return result


def _stock_change(code, before_item, after_item, previous, current, interval_seconds):
    before_quote = (before_item or {}).get("quote") or {}
    after_quote = (after_item or {}).get("quote") or {}
    before_intraday = (before_item or {}).get("intraday") or {}
    after_intraday = (after_item or {}).get("intraday") or {}
    reasons = []

    price_fields = {}
    for field in ("latest", "change_percent", "high", "low", "amplitude_percent"):
        price_fields[field] = _numeric_change(before_quote.get(field), after_quote.get(field))

    latest = price_fields["latest"]
    latest_price_delta_pct = latest.get("delta_percent_of_before")
    latest_sev = _severity_for(latest_price_delta_pct, "stock_price_delta_percent_abs")
    _add_reason(reasons, latest_sev, "LATEST_PRICE_MOVED", latest_price_delta_pct)

    change_pct_delta = price_fields["change_percent"].get("delta")
    change_sev = _severity_for(change_pct_delta, "stock_change_percent_delta_abs")
    _add_reason(reasons, change_sev, "CHANGE_PERCENT_MOVED", change_pct_delta)

    before_date = _market_date(before_item)
    after_date = _market_date(after_item)
    same_session = bool(before_date and after_date and before_date == after_date)
    amount_change = _numeric_change(before_quote.get("amount_1e8"), after_quote.get("amount_1e8"))
    amount_delta_per_minute = None
    amount_flags = []
    if same_session and amount_change.get("delta") is not None and interval_seconds and interval_seconds > 0:
        amount_delta_per_minute = amount_change["delta"] / interval_seconds * 60.0
        amount_sev = _severity_for(amount_change.get("delta_percent_of_before"), "turnover_delta_percent_abs")
        _add_reason(reasons, amount_sev, "CUMULATIVE_TURNOVER_CHANGED", amount_change.get("delta_percent_of_before"))
    elif before_date and after_date and before_date != after_date:
        amount_flags.append("MARKET_SESSION_RESET")
        amount_change["delta"] = None
        amount_change["delta_percent_of_before"] = None
        amount_change["comparable"] = False

    intraday_numeric = {}
    for field in (
        "trend_5m_percent",
        "trend_15m_percent",
        "trend_30m_percent",
        "price_vs_vwap_percent",
        "day_range_position_percent",
        "volume_strength_ratio_5m",
        "amount_strength_ratio_5m",
    ):
        intraday_numeric[field] = _numeric_change(before_intraday.get(field), after_intraday.get(field))
        delta = intraday_numeric[field].get("delta")
        if field in {"trend_5m_percent", "trend_15m_percent", "trend_30m_percent", "price_vs_vwap_percent"}:
            sev = _severity_for(delta, "intraday_metric_delta_abs")
            _add_reason(reasons, sev, f"{field.upper()}_CHANGED", delta)

    intraday_states = {
        "bias": _state_change(before_intraday.get("bias"), after_intraday.get("bias")),
        "structure": _state_change(before_intraday.get("structure"), after_intraday.get("structure")),
        "above_vwap": _state_change(before_intraday.get("above_vwap"), after_intraday.get("above_vwap")),
    }
    if intraday_states["bias"]["changed"]:
        _add_reason(reasons, "MODERATE", "INTRADAY_BIAS_CHANGED", intraday_states["bias"])
    if intraday_states["above_vwap"]["changed"]:
        _add_reason(reasons, "MINOR", "VWAP_SIDE_CHANGED", intraday_states["above_vwap"])

    before_relative, before_driver = _stock_relative(previous, code)
    after_relative, after_driver = _stock_relative(current, code)
    relative = {
        "vs_market_percent": _numeric_change(
            before_relative.get("vs_market_percent") if "vs_market_percent" in before_relative else before_relative.get("vs_market_mean_percent"),
            after_relative.get("vs_market_percent") if "vs_market_percent" in after_relative else after_relative.get("vs_market_mean_percent"),
        ),
        "vs_group_mean_percent": _numeric_change(before_relative.get("vs_group_mean_percent"), after_relative.get("vs_group_mean_percent")),
        "relative_to_market": _state_change(before_relative.get("relative_to_market"), after_relative.get("relative_to_market")),
        "relative_to_group": _state_change(before_relative.get("relative_to_group"), after_relative.get("relative_to_group")),
        "primary_driver": _state_change(before_driver.get("primary_driver"), after_driver.get("primary_driver")),
    }
    rel_delta = relative["vs_market_percent"].get("delta")
    rel_sev = _severity_for(rel_delta, "relative_strength_delta_abs")
    _add_reason(reasons, rel_sev, "RELATIVE_TO_MARKET_CHANGED", rel_delta)
    group_rel_delta = relative["vs_group_mean_percent"].get("delta")
    group_rel_sev = _severity_for(group_rel_delta, "relative_strength_delta_abs")
    _add_reason(reasons, group_rel_sev, "RELATIVE_TO_GROUP_CHANGED", group_rel_delta)
    if relative["primary_driver"]["changed"]:
        _add_reason(reasons, "MODERATE", "DRIVER_ATTRIBUTION_CHANGED", relative["primary_driver"])

    window_changes = _relative_window_changes(before_item, after_item)
    for kind in ("vs_groups", "vs_indices"):
        for benchmark_id, benchmark in window_changes[kind].items():
            for window_id, window in (benchmark.get("windows") or {}).items():
                delta = (window.get("excess_return_percent") or {}).get("delta")
                severity = _severity_for(delta, "relative_strength_delta_abs")
                _add_reason(
                    reasons,
                    severity,
                    f"SYNCED_EXCESS_RETURN_CHANGED:{kind}:{benchmark_id}:{window_id}",
                    delta,
                )
                state = window.get("state") or {}
                if state.get("changed"):
                    _add_reason(
                        reasons,
                        "MODERATE",
                        f"SYNCED_RELATIVE_STATE_CHANGED:{kind}:{benchmark_id}:{window_id}",
                        state,
                    )

    significance = "NONE"
    for reason in reasons:
        significance = _max_severity(significance, reason["severity"])

    strength_delta = group_rel_delta if group_rel_delta is not None else rel_delta
    if strength_delta is None:
        strength_direction = "UNKNOWN"
    elif strength_delta >= THRESHOLDS["relative_strength_delta_abs"]["MODERATE"]:
        strength_direction = "STRONGER"
    elif strength_delta <= -THRESHOLDS["relative_strength_delta_abs"]["MODERATE"]:
        strength_direction = "WEAKER"
    else:
        strength_direction = "UNCHANGED"

    return {
        "code": code,
        "status_before": (before_item or {}).get("status"),
        "status_after": (after_item or {}).get("status"),
        "price_change": price_fields,
        "turnover_change": {
            "amount_1e8": amount_change,
            "incremental_amount_1e8": _round(amount_change.get("delta"), 4) if same_session else None,
            "incremental_amount_per_minute_1e8": _round(amount_delta_per_minute, 5),
            "same_market_session": same_session,
            "market_session_before": before_date,
            "market_session_after": after_date,
            "quality_flags": amount_flags,
        },
        "intraday_change": {
            "numeric": intraday_numeric,
            "states": intraday_states,
        },
        "relative_strength_change": relative,
        "relative_strength_windows_change": window_changes,
        "strength_direction": strength_direction,
        "significance": significance,
        "significance_reasons": reasons,
    }


def _group_rank(group):
    if not isinstance(group, dict):
        return None, None
    target = group.get("target") or {}
    target_code = target.get("code")
    target_pct = _as_float(target.get("change_percent"))
    if not target_code or target_pct is None:
        return None, None

    rows = [(str(target_code), target_pct)]
    for member in group.get("members") or []:
        if not isinstance(member, dict) or not member.get("available"):
            continue
        code = member.get("code")
        pct = _as_float(member.get("change_percent"))
        if code and pct is not None and str(code) != str(target_code):
            rows.append((str(code), pct))
    rows.sort(key=lambda x: (-x[1], x[0]))
    for idx, (code, _) in enumerate(rows, start=1):
        if code == str(target_code):
            return idx, len(rows)
    return None, len(rows)


def _group_change(group_id, before, after):
    reasons = []
    metrics = {}
    for field in (
        "mean_change_percent",
        "median_change_percent",
        "breadth_score_percent",
        "coverage_percent",
        "target_vs_peer_mean_percent",
    ):
        metrics[field] = _numeric_change((before or {}).get(field), (after or {}).get(field))

    breadth_delta = metrics["breadth_score_percent"].get("delta")
    breadth_sev = _severity_for(breadth_delta, "group_breadth_delta_abs")
    _add_reason(reasons, breadth_sev, "GROUP_BREADTH_CHANGED", breadth_delta)

    rank_before, peer_count_before = _group_rank(before)
    rank_after, peer_count_after = _group_rank(after)
    rank_improvement = rank_before - rank_after if rank_before is not None and rank_after is not None else None
    rank_sev = _severity_for(rank_improvement, "group_rank_change_abs")
    _add_reason(reasons, rank_sev, "TARGET_GROUP_RANK_CHANGED", rank_improvement)

    status_change = _state_change((before or {}).get("status"), (after or {}).get("status"))
    if status_change["changed"]:
        _add_reason(reasons, "MINOR", "GROUP_STATUS_CHANGED", status_change)

    significance = "NONE"
    for reason in reasons:
        significance = _max_severity(significance, reason["severity"])

    return {
        "group_id": group_id,
        "status": status_change,
        "metrics": metrics,
        "target_rank": {
            "before": rank_before,
            "after": rank_after,
            "rank_improvement": rank_improvement,
            "peer_count_before": peer_count_before,
            "peer_count_after": peer_count_after,
        },
        "significance": significance,
        "significance_reasons": reasons,
    }


def _breadth_overall(snapshot):
    return (((snapshot.get("market_environment") or {}).get("breadth") or {}).get("overall") or {})


def _breadth_meta(snapshot):
    breadth = ((snapshot.get("market_environment") or {}).get("breadth") or {})
    return {
        "status": breadth.get("status"),
        "estimated": breadth.get("estimated"),
        "market_session_date": breadth.get("market_session_date"),
        "source": breadth.get("source"),
    }


def _market_change(previous, current):
    reasons = []
    before_indices = previous.get("indices") or {}
    after_indices = current.get("indices") or {}
    indices = {}
    for name in sorted(set(before_indices) | set(after_indices)):
        before_quote = ((before_indices.get(name) or {}).get("quote") or {})
        after_quote = ((after_indices.get(name) or {}).get("quote") or {})
        change = _numeric_change(before_quote.get("change_percent"), after_quote.get("change_percent"))
        sev = _severity_for(change.get("delta"), "index_change_delta_abs")
        _add_reason(reasons, sev, f"INDEX_{name}_CHANGE_PERCENT_MOVED", change.get("delta"))
        indices[name] = {
            "change_percent": change,
            "freshness": _state_change(before_quote.get("freshness"), after_quote.get("freshness")),
        }

    before_env = previous.get("market_environment") or {}
    after_env = current.get("market_environment") or {}
    regime = _state_change(((before_env.get("regime") or {}).get("status")), ((after_env.get("regime") or {}).get("status")))
    style = _state_change(((before_env.get("style") or {}).get("status")), ((after_env.get("style") or {}).get("status")))
    if regime["changed"]:
        _add_reason(reasons, "MODERATE", "MARKET_REGIME_CHANGED", regime)
    if style["changed"]:
        _add_reason(reasons, "MODERATE", "MARKET_STYLE_CHANGED", style)

    before_breadth = _breadth_overall(previous)
    after_breadth = _breadth_overall(current)
    before_breadth_meta = _breadth_meta(previous)
    after_breadth_meta = _breadth_meta(current)
    breadth = {}
    for field in ("up_ratio_percent", "down_ratio_percent", "breadth_score_percent"):
        breadth[field] = _numeric_change(before_breadth.get(field), after_breadth.get(field))
    breadth_delta = breadth["breadth_score_percent"].get("delta")
    breadth_sev = _severity_for(breadth_delta, "market_breadth_delta_abs")
    _add_reason(reasons, breadth_sev, "MARKET_BREADTH_CHANGED", breadth_delta)

    count_comparable = (
        before_breadth_meta.get("estimated") == after_breadth_meta.get("estimated")
        and before_breadth_meta.get("status") in {"OK", "PARTIAL"}
        and after_breadth_meta.get("status") in {"OK", "PARTIAL"}
    )
    counts = {}
    for field in ("up_count", "down_count", "flat_count", "unavailable_change_count"):
        value = _numeric_change(before_breadth.get(field), after_breadth.get(field))
        if not count_comparable:
            value["delta"] = None
            value["delta_percent_of_before"] = None
            value["comparable"] = False
        counts[field] = value

    turnover = _numeric_change(before_breadth.get("amount_1e8"), after_breadth.get("amount_1e8"))
    same_market_session = bool(
        before_breadth_meta.get("market_session_date")
        and before_breadth_meta.get("market_session_date") == after_breadth_meta.get("market_session_date")
    )
    if not same_market_session:
        turnover["delta"] = None
        turnover["delta_percent_of_before"] = None
        turnover["comparable"] = False

    before_spreads = (before_env.get("style") or {}).get("spreads") or {}
    after_spreads = (after_env.get("style") or {}).get("spreads") or {}
    style_spreads = {
        key: _numeric_change(before_spreads.get(key), after_spreads.get(key))
        for key in sorted(set(before_spreads) | set(after_spreads))
    }

    significance = "NONE"
    for reason in reasons:
        significance = _max_severity(significance, reason["severity"])

    return {
        "indices": indices,
        "regime": regime,
        "style": style,
        "style_spreads": style_spreads,
        "breadth": {
            "metadata_before": before_breadth_meta,
            "metadata_after": after_breadth_meta,
            "ratios": breadth,
            "counts": counts,
        },
        "turnover": {
            "amount_1e8": turnover,
            "same_market_session": same_market_session,
        },
        "significance": significance,
        "significance_reasons": reasons,
        "available": bool(before_env and after_env),
    }


def _event_items(item):
    events = (item or {}).get("events")
    if isinstance(events, list):
        candidates = events
    elif isinstance(events, dict):
        candidates = []
        for key in ("recent", "upcoming"):
            value = events.get(key)
            if isinstance(value, list):
                candidates.extend(value)
        latest = events.get("latest")
        if isinstance(latest, dict):
            candidates.append(latest)
        elif isinstance(latest, list):
            candidates.extend(latest)
    else:
        candidates = []

    out = {}
    for event in candidates:
        if not isinstance(event, dict):
            continue
        event_id = event.get("event_id")
        if event_id:
            out[str(event_id)] = event
    return out


def _event_comparable_view(event):
    keys = (
        "event_type",
        "title",
        "published_at",
        "effective_date",
        "importance",
        "facts",
        "status",
        "source_url",
        "related_event_id",
        "supersedes_event_id",
    )
    return {key: event.get(key) for key in keys if key in event}


def _event_changes(previous, current):
    new_events = []
    updated_events = []
    closed_events = []
    per_stock = {}
    previous_detail = previous.get("detail_stocks") or {}
    current_detail = current.get("detail_stocks") or {}

    for code in sorted(set(previous_detail) | set(current_detail)):
        before_map = _event_items(previous_detail.get(code))
        after_map = _event_items(current_detail.get(code))
        code_new = []
        code_updated = []
        code_closed = []

        for event_id, event in after_map.items():
            if event_id not in before_map:
                entry = {
                    "event_id": event_id,
                    "code": code,
                    "event_type": event.get("event_type") or event.get("type"),
                    "title": event.get("title"),
                    "importance": event.get("importance"),
                    "published_at": event.get("published_at"),
                }
                code_new.append(entry)
                new_events.append(entry)
                continue

            before_event = before_map[event_id]
            if _event_comparable_view(before_event) != _event_comparable_view(event):
                entry = {
                    "event_id": event_id,
                    "code": code,
                    "before": _event_comparable_view(before_event),
                    "after": _event_comparable_view(event),
                }
                code_updated.append(entry)
                updated_events.append(entry)

            before_status = str(before_event.get("status") or "").upper()
            after_status = str(event.get("status") or "").upper()
            if before_status not in {"CLOSED", "COMPLETED", "CANCELLED"} and after_status in {"CLOSED", "COMPLETED", "CANCELLED"}:
                entry = {
                    "event_id": event_id,
                    "code": code,
                    "before_status": before_event.get("status"),
                    "after_status": event.get("status"),
                }
                code_closed.append(entry)
                closed_events.append(entry)

        if code_new or code_updated or code_closed:
            per_stock[code] = {
                "new": code_new,
                "updated": code_updated,
                "closed": code_closed,
            }

    significant_new = [event for event in new_events if str(event.get("importance") or "").upper() == "HIGH"]
    if significant_new:
        significance = "SIGNIFICANT"
    elif new_events or updated_events or closed_events:
        significance = "MODERATE"
    else:
        significance = "NONE"

    return {
        "status": "AVAILABLE" if any(_event_items(item) for item in current_detail.values()) or any(_event_items(item) for item in previous_detail.values()) else "NO_EVENT_LAYER",
        "new": new_events,
        "updated": updated_events,
        "closed": closed_events,
        "by_stock": per_stock,
        "significance": significance,
    }


def build_changes(previous, current, previous_path=None):
    if not isinstance(previous, dict):
        return {
            "status": "NO_BASELINE",
            "baseline": {
                "previous_snapshot_path": previous_path,
                "previous_snapshot_time": None,
                "current_snapshot_time": (_snapshot_time(current).isoformat(timespec="seconds") if _snapshot_time(current) else current.get("runner_time_cst")),
                "interval_seconds": None,
                "previous_observation": None,
                "current_observation": {
                    "run_id": ((current.get("observation") or {}).get("run_id")),
                    "run_attempt": ((current.get("observation") or {}).get("run_attempt")),
                    "head_sha": ((current.get("observation") or {}).get("head_sha")),
                },
                "quality": "MISSING",
                "quality_flags": ["NO_VALID_PREVIOUS_SNAPSHOT"],
            },
            "thresholds": THRESHOLDS,
            "market": None,
            "stocks": {},
            "groups": {},
            "events": {"status": "NO_BASELINE", "new": [], "updated": [], "closed": [], "by_stock": {}, "significance": "NONE"},
            "summary": {
                "significant_changes": 0,
                "moderate_changes": 0,
                "minor_changes": 0,
                "new_events": 0,
                "stronger_stocks": [],
                "weaker_stocks": [],
            },
        }

    baseline = _baseline_descriptor(previous, current, previous_path)
    interval = _as_float(baseline.get("interval_seconds"))
    current_detail = current.get("detail_stocks") or {}
    previous_detail = previous.get("detail_stocks") or {}
    stocks = {}
    for code in sorted(set(current_detail) | set(previous_detail)):
        stocks[code] = _stock_change(
            code,
            previous_detail.get(code),
            current_detail.get(code),
            previous,
            current,
            interval,
        )

    groups = {}
    previous_groups = previous.get("groups") or {}
    current_groups = current.get("groups") or {}
    for group_id in sorted(set(previous_groups) | set(current_groups)):
        groups[group_id] = _group_change(group_id, previous_groups.get(group_id), current_groups.get(group_id))

    market = _market_change(previous, current)
    events = _event_changes(previous, current)

    significant = 0
    moderate = 0
    minor = 0
    for item in list(stocks.values()) + list(groups.values()) + [market, events]:
        severity = item.get("significance") or "NONE"
        if severity == "SIGNIFICANT":
            significant += 1
        elif severity == "MODERATE":
            moderate += 1
        elif severity == "MINOR":
            minor += 1

    stronger = sorted(code for code, item in stocks.items() if item.get("strength_direction") == "STRONGER")
    weaker = sorted(code for code, item in stocks.items() if item.get("strength_direction") == "WEAKER")

    status = "OK" if baseline.get("quality") == "OK" else "PARTIAL"
    return {
        "status": status,
        "baseline": baseline,
        "thresholds": THRESHOLDS,
        "market": market,
        "stocks": stocks,
        "groups": groups,
        "events": events,
        "summary": {
            "significant_changes": significant,
            "moderate_changes": moderate,
            "minor_changes": minor,
            "new_events": len(events.get("new") or []),
            "updated_events": len(events.get("updated") or []),
            "closed_events": len(events.get("closed") or []),
            "stronger_stocks": stronger,
            "weaker_stocks": weaker,
        },
    }


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    current = json.loads(path.read_text(encoding="utf-8"))
    previous, previous_path = history_store.load_previous_snapshot(current)
    current["changes_since_previous"] = build_changes(previous, current, previous_path)
    current["schema_version"] = max(int(current.get("schema_version") or 0), 11)
    current.setdefault("features", {})["changes_since_previous"] = "v1"
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

    changes = current["changes_since_previous"]
    summary = changes.get("summary") or {}
    print(
        "CHANGES_SINCE_PREVIOUS "
        f"status={changes.get('status')} interval={((changes.get('baseline') or {}).get('interval_seconds'))} "
        f"significant={summary.get('significant_changes')} moderate={summary.get('moderate_changes')} "
        f"new_events={summary.get('new_events')}",
        flush=True,
    )
    print("SNAPSHOT_SCHEMA_UPGRADED schema_version=11 feature=changes_since_previous:v1", flush=True)
