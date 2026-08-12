import json
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import market_calendar
import minute_history


WINDOWS = (5, 15, 30)
EXCESS_STATE_THRESHOLD_PERCENT = 0.15
INDEX_BENCHMARKS = {
    "chinext": {"name": "创业板指", "tcode": "sz399006"},
    "csi1000": {"name": "中证1000", "tcode": "sh000852"},
}
MAX_BENCHMARK_SERIES = 52


def _round(value, digits=4):
    return None if value is None else round(float(value), digits)


def _session_date(value):
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _time_label(value):
    text = str(value or "").replace(":", "").strip()
    try:
        datetime.strptime(text, "%H%M")
        return text
    except ValueError:
        return None


def _series(date_value, points, expected_session):
    actual_session = _session_date(date_value)
    by_time = {}
    duplicates = set()
    invalid = 0
    for raw in points or []:
        if not isinstance(raw, dict):
            invalid += 1
            continue
        label = _time_label(raw.get("time"))
        try:
            price = float(raw.get("price"))
        except (TypeError, ValueError):
            invalid += 1
            continue
        if label is None or price <= 0:
            invalid += 1
            continue
        if label in by_time:
            duplicates.add(label)
        by_time[label] = price
    reasons = []
    if actual_session != expected_session:
        reasons.append("SESSION_DATE_MISMATCH")
    if duplicates:
        reasons.append("DUPLICATE_MINUTE_TIMES")
    if invalid:
        reasons.append("INVALID_MINUTE_ROWS")
    return {
        "session_date": actual_session,
        "prices": by_time,
        "duplicate_times": sorted(duplicates),
        "invalid_row_count": invalid,
        "reason_codes": reasons,
    }


def _window_return(series, labels, start, end):
    if not isinstance(series, dict):
        return {
            "status": "UNAVAILABLE",
            "return_percent": None,
            "missing_times": list(labels),
            "reason_codes": ["SERIES_UNAVAILABLE"],
        }
    reasons = list(series.get("reason_codes") or [])
    prices = series.get("prices") or {}
    missing = [label for label in labels if label not in prices]
    if missing:
        reasons.append("MISSING_WINDOW_MINUTES")
    if reasons:
        return {
            "status": "GAPPED" if missing else "UNAVAILABLE",
            "return_percent": None,
            "missing_times": missing,
            "reason_codes": list(dict.fromkeys(reasons)),
        }
    value = (prices[end] / prices[start] - 1.0) * 100.0
    return {
        "status": "OK",
        "return_percent": _round(value),
        "missing_times": [],
        "reason_codes": [],
    }


def _state(excess):
    if excess is None:
        return "UNVERIFIED"
    if excess > EXCESS_STATE_THRESHOLD_PERCENT:
        return "OUTPERFORMING"
    if excess < -EXCESS_STATE_THRESHOLD_PERCENT:
        return "UNDERPERFORMING"
    return "IN_LINE"


def _base_window(start, end, target_result):
    return {
        "window_start": start,
        "window_end": end,
        "cutoff": end,
        "target_return_percent": target_result.get("return_percent"),
        "benchmark_return_percent": None,
        "excess_return_percent": None,
        "state": "UNVERIFIED",
        "threshold_percent": EXCESS_STATE_THRESHOLD_PERCENT,
    }


def _index_window(target_result, benchmark_series, labels, start, end):
    benchmark = _window_return(benchmark_series, labels, start, end)
    result = _base_window(start, end, target_result)
    reasons = list(target_result.get("reason_codes") or []) + list(benchmark.get("reason_codes") or [])
    target_return = target_result.get("return_percent")
    benchmark_return = benchmark.get("return_percent")
    excess = (
        target_return - benchmark_return
        if target_return is not None and benchmark_return is not None
        else None
    )
    if target_result.get("status") == "OK" and benchmark.get("status") == "OK":
        status = "OK"
        quality = "PASS"
    elif "GAPPED" in {target_result.get("status"), benchmark.get("status")}:
        status = "GAPPED"
        quality = "DEGRADED"
    else:
        status = "UNAVAILABLE"
        quality = "FAILED"
    result.update(
        {
            "status": status,
            "quality": quality,
            "benchmark_return_percent": benchmark_return,
            "excess_return_percent": _round(excess),
            "state": _state(excess),
            "coverage": {
                "target": target_result,
                "benchmark": benchmark,
            },
            "reason_codes": list(dict.fromkeys(reasons)),
        }
    )
    return result


def _group_window(target_result, peer_series, requested_codes, labels, start, end):
    peer_results = {
        code: _window_return(peer_series.get(code), labels, start, end)
        for code in requested_codes
    }
    returns = {
        code: item["return_percent"]
        for code, item in peer_results.items()
        if item.get("return_percent") is not None
    }
    values = list(returns.values())
    median = statistics.median(values) if values else None
    mean = statistics.fmean(values) if values else None
    target_return = target_result.get("return_percent")
    excess = target_return - median if target_return is not None and median is not None else None
    requested = len(requested_codes)
    covered = len(returns)
    coverage_percent = covered / requested * 100.0 if requested else 0.0
    result = _base_window(start, end, target_result)
    if target_result.get("status") != "OK":
        status = target_result.get("status") or "UNAVAILABLE"
        quality = "DEGRADED" if status == "GAPPED" else "FAILED"
        reasons = list(target_result.get("reason_codes") or [])
    elif covered == requested and requested > 0:
        status = "OK"
        quality = "PASS"
        reasons = []
    elif covered > 0:
        status = "PARTIAL"
        quality = "DEGRADED"
        reasons = ["PEER_WINDOW_COVERAGE_PARTIAL"]
    else:
        status = "UNAVAILABLE"
        quality = "FAILED"
        reasons = ["PEER_WINDOW_UNAVAILABLE"]
    result.update(
        {
            "status": status,
            "quality": quality,
            "benchmark_return_percent": _round(median),
            "benchmark_mean_return_percent": _round(mean),
            "excess_return_percent": _round(excess),
            "state": _state(excess),
            "coverage": {
                "target": target_result,
                "requested_peer_count": requested,
                "covered_peer_count": covered,
                "peer_coverage_percent": _round(coverage_percent, 2),
                "requested_peer_codes": list(requested_codes),
                "covered_peer_codes": sorted(returns),
                "aggregation_method": "MEDIAN_EQUAL_WEIGHT",
                "benchmark_mean_reported": True,
                "peer_results": peer_results,
            },
            "reason_codes": reasons,
        }
    )
    return result


def _unavailable_windows(reason, expected_times):
    cutoff = expected_times[-1] if expected_times else None
    return {
        f"{window}m": {
            "status": "UNAVAILABLE",
            "quality": "FAILED",
            "window_start": None,
            "window_end": cutoff,
            "cutoff": cutoff,
            "target_return_percent": None,
            "benchmark_return_percent": None,
            "excess_return_percent": None,
            "state": "UNVERIFIED",
            "threshold_percent": EXCESS_STATE_THRESHOLD_PERCENT,
            "coverage": None,
            "reason_codes": [reason],
        }
        for window in WINDOWS
    }


def _fetch_benchmarks(base, codes):
    result = {}

    def fetch(tcode):
        date_value, rows = base.tencent_minutes(tcode)
        return date_value, base.parse_minutes(rows)

    workers = min(8, max(1, len(codes)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, tcode): tcode for tcode in codes}
        for future in as_completed(futures):
            tcode = futures[future]
            try:
                date_value, points = future.result()
                result[tcode] = {"date": date_value, "points": points, "error": None}
            except Exception as exc:
                result[tcode] = {
                    "date": None,
                    "points": [],
                    "error": f"{type(exc).__name__}: {exc}",
                }
    return result


def build_relative_strength(snapshot, config, raw_benchmarks, base):
    runner_time = minute_history._snapshot_time(snapshot)
    calendar_expectation = market_calendar.expected_minute_times(runner_time)
    expected_times = list(calendar_expectation.get("expected_times") or [])
    session_date = calendar_expectation.get("session_date")
    detail = snapshot.get("detail_stocks") or {}
    output = {}

    peer_tcodes = {}
    for group in (config.get("groups") or {}).values():
        for code in group.get("active_member_codes") or []:
            try:
                _, _, tcode = base.infer_identifiers(code)
                peer_tcodes[code] = tcode
            except Exception:
                continue
    series_by_tcode = {}
    for tcode, raw in raw_benchmarks.items():
        series = _series(raw.get("date"), raw.get("points"), session_date)
        if raw.get("error"):
            series["reason_codes"].append("PROVIDER_ERROR")
            series["provider_error"] = raw.get("error")
        series_by_tcode[tcode] = series

    for code in detail:
        try:
            target_document = minute_history.load_from_snapshot(snapshot, code)
            target_series = _series(
                target_document.get("session_date"),
                target_document.get("points"),
                session_date,
            )
        except Exception as exc:
            target_series = {
                "session_date": None,
                "prices": {},
                "reason_codes": ["TARGET_MINUTE_HISTORY_UNAVAILABLE"],
                "error": f"{type(exc).__name__}: {exc}",
            }

        groups = {}
        for group_id, group in (config.get("groups") or {}).items():
            if group.get("target_code") != code:
                continue
            requested_codes = list(group.get("member_codes") or [])
            peer_series = {
                peer_code: series_by_tcode.get(peer_tcodes.get(peer_code))
                for peer_code in requested_codes
            }
            groups[group_id] = {
                "benchmark_type": "CONFIGURED_PEER_BASKET",
                "label": group.get("label") or group_id,
                "windows": {},
            }
            for window in WINDOWS:
                key = f"{window}m"
                if len(expected_times) < window + 1:
                    groups[group_id]["windows"][key] = _unavailable_windows(
                        "INSUFFICIENT_COMPLETED_MINUTES", expected_times
                    )[key]
                    continue
                labels = expected_times[-(window + 1) :]
                target_result = _window_return(target_series, labels, labels[0], labels[-1])
                groups[group_id]["windows"][key] = _group_window(
                    target_result,
                    peer_series,
                    requested_codes,
                    labels,
                    labels[0],
                    labels[-1],
                )

        indices = {}
        for benchmark_id, benchmark in INDEX_BENCHMARKS.items():
            indices[benchmark_id] = {
                "benchmark_type": "MARKET_INDEX",
                "name": benchmark["name"],
                "tcode": benchmark["tcode"],
                "windows": {},
            }
            benchmark_series = series_by_tcode.get(benchmark["tcode"])
            for window in WINDOWS:
                key = f"{window}m"
                if len(expected_times) < window + 1:
                    indices[benchmark_id]["windows"][key] = _unavailable_windows(
                        "INSUFFICIENT_COMPLETED_MINUTES", expected_times
                    )[key]
                    continue
                labels = expected_times[-(window + 1) :]
                target_result = _window_return(target_series, labels, labels[0], labels[-1])
                indices[benchmark_id]["windows"][key] = _index_window(
                    target_result,
                    benchmark_series,
                    labels,
                    labels[0],
                    labels[-1],
                )

        all_windows = [
            value
            for container in list(groups.values()) + list(indices.values())
            for value in (container.get("windows") or {}).values()
        ]
        pass_count = sum(item.get("quality") == "PASS" for item in all_windows)
        if pass_count == len(all_windows) and all_windows:
            status = "OK"
            quality = "PASS"
        elif pass_count or any(item.get("excess_return_percent") is not None for item in all_windows):
            status = "PARTIAL"
            quality = "DEGRADED"
        else:
            status = "UNAVAILABLE"
            quality = "FAILED"
        output[code] = {
            "schema_version": 1,
            "status": status,
            "session_date": session_date,
            "cutoff": expected_times[-1] if expected_times else None,
            "forming_minute_excluded": calendar_expectation.get("forming_minute"),
            "windows": [f"{value}m" for value in WINDOWS],
            "vs_groups": groups,
            "vs_indices": indices,
            "metadata": {
                "freshness": "DERIVED_CURRENT",
                "quality": quality,
                "calendar_status": calendar_expectation.get("status"),
                "verification_status": calendar_expectation.get("verification_status"),
            },
            "provenance": {
                "type": "DERIVED",
                "target_source": "minute_history canonical current-session record",
                "benchmark_source": "Tencent 1-minute cumulative series",
                "algorithm": "synchronized_window_return_v1",
                "aggregation_method": "MEDIAN_EQUAL_WEIGHT for peer basket",
                "calendar_version": market_calendar.load_calendar()["calendar_version"],
            },
        }
    return output


def finalize_snapshot(snapshot_path, base, config):
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    tcodes = set()
    for group in (config.get("groups") or {}).values():
        for code in group.get("active_member_codes") or []:
            _, _, tcode = base.infer_identifiers(code)
            tcodes.add(tcode)
    tcodes.update(item["tcode"] for item in INDEX_BENCHMARKS.values())
    if len(tcodes) > MAX_BENCHMARK_SERIES:
        raise RuntimeError(
            f"relative strength benchmark cap exceeded: {len(tcodes)}/{MAX_BENCHMARK_SERIES}"
        )
    raw = _fetch_benchmarks(base, sorted(tcodes))
    results = build_relative_strength(snapshot, config, raw, base)
    for code, value in results.items():
        if code in (snapshot.get("detail_stocks") or {}):
            snapshot["detail_stocks"][code]["relative_strength_windows"] = value
    snapshot["schema_version"] = max(int(snapshot.get("schema_version") or 0), 21)
    snapshot.setdefault("features", {})["relative_strength_windows"] = "v1"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "RELATIVE_STRENGTH_WINDOWS "
        + ",".join(
            f"{code}:{value.get('status')}@{value.get('cutoff')}"
            for code, value in sorted(results.items())
        ),
        flush=True,
    )
