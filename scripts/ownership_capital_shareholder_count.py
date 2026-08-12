import json
from datetime import date
from pathlib import Path

import ownership_capital_base as core

HISTORY_LIMIT = 12
SECTION_KEYS = ("gdrs", "gdhs", "shareholder_count")
STABLE_BAND_PERCENT = 1.0


def _first(row, *names):
    for name in names:
        value = row.get(name)
        if value not in (None, "", "-"):
            return value
    return None


def _as_int(value):
    try:
        return None if value in (None, "", "-") else int(float(value))
    except (TypeError, ValueError):
        return None


def fetch_shareholder_count(base, code):
    provider_code = core._provider_code(base, code)
    payload, url = core._fetch_json_object(base, core.SHAREHOLDER_RESEARCH_ENDPOINT, provider_code)
    return payload, url, provider_code


def _rows(payload):
    if not isinstance(payload, dict):
        return [], None
    for key in SECTION_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value, key
    return [], None


def _normalize_row(row):
    if not isinstance(row, dict):
        return None
    report_date = core._date(_first(row, "END_DATE", "REPORT_DATE", "STAT_DATE"))
    if not report_date:
        return None
    return {
        "report_date": report_date,
        "shareholder_count": _as_int(_first(row, "HOLDER_TOTAL_NUM", "HOLDER_NUM", "TOTAL_NUM")),
        "provider_period_change_percent": core._as_float(_first(row, "TOTAL_NUM_RATIO", "HOLDER_NUM_RATIO")),
        "average_free_shares": core._as_float(_first(row, "AVG_FREE_SHARES", "AVG_FREESHARES")),
        "provider_average_free_shares_change_percent": core._as_float(
            _first(row, "AVG_FREESHARES_RATIO", "AVG_FREE_SHARES_RATIO")
        ),
        "provider_focus": _first(row, "HOLD_FOCUS", "HOLDER_FOCUS"),
        "price": core._as_float(_first(row, "PRICE", "CLOSE_PRICE")),
        "average_holding_amount": core._as_float(_first(row, "AVG_HOLD_AMT", "AVG_HOLD_AMOUNT")),
        "top10_concentration_percent": core._as_float(_first(row, "HOLD_RATIO_TOTAL", "TOP10_HOLD_RATIO")),
        "float_top10_concentration_percent": core._as_float(
            _first(row, "FREEHOLD_RATIO_TOTAL", "FREE_HOLD_RATIO_TOTAL")
        ),
        "change_from_previous": None,
    }


def _history(rows):
    values = {}
    dropped = 0
    for raw in rows:
        row = _normalize_row(raw)
        if row is None:
            dropped += 1
            continue
        old = values.get(row["report_date"])
        if old is None or (old["shareholder_count"] is None and row["shareholder_count"] is not None):
            values[row["report_date"]] = row
    return [values[key] for key in sorted(values, reverse=True)][:HISTORY_LIMIT], dropped


def _attach_period_changes(history):
    for index, current in enumerate(history[:-1]):
        previous = history[index + 1]
        current_count = current.get("shareholder_count")
        previous_count = previous.get("shareholder_count")
        if current_count is None or previous_count in (None, 0):
            continue
        pct = core._round((current_count - previous_count) / previous_count * 100.0, 4)
        provider_pct = current.get("provider_period_change_percent")
        current["change_from_previous"] = {
            "previous_report_date": previous["report_date"],
            "shareholder_count_delta": current_count - previous_count,
            "shareholder_count_change_percent": pct,
            "provider_change_percent": provider_pct,
            "provider_delta_gap_pp": core._round(provider_pct - pct, 4) if provider_pct is not None else None,
        }


def _target_date(value, months):
    import calendar

    index = value.year * 12 + value.month - 1 - months
    year, month0 = divmod(index, 12)
    month = month0 + 1
    src_end = calendar.monthrange(value.year, value.month)[1]
    dst_end = calendar.monthrange(year, month)[1]
    day = dst_end if value.day == src_end else min(value.day, dst_end)
    return date(year, month, day)


def _window(history, months):
    latest = next((row for row in history if row.get("shareholder_count") is not None), None)
    if latest is None:
        return None
    target = _target_date(date.fromisoformat(latest["report_date"]), months)
    baseline = next(
        (
            row for row in history[1:]
            if row.get("shareholder_count") not in (None, 0)
            and date.fromisoformat(row["report_date"]) <= target
        ),
        None,
    )
    if baseline is None:
        return None
    current = latest["shareholder_count"]
    old = baseline["shareholder_count"]
    return {
        "months": months,
        "latest_report_date": latest["report_date"],
        "baseline_report_date": baseline["report_date"],
        "latest_shareholder_count": current,
        "baseline_shareholder_count": old,
        "shareholder_count_delta": current - old,
        "shareholder_count_change_percent": core._round((current - old) / old * 100.0, 4),
        "baseline_policy": "LATEST_DISCLOSED_OBSERVATION_ON_OR_BEFORE_TARGET_DATE",
    }


def _trend(windows):
    values = [
        item["shareholder_count_change_percent"]
        for item in windows.values()
        if item is not None
    ]
    if not values:
        return "UNKNOWN"
    material = [value for value in values if abs(value) > STABLE_BAND_PERCENT]
    if not material:
        return "STABLE"
    positive = any(value > 0 for value in material)
    negative = any(value < 0 for value in material)
    if positive and negative:
        return "VOLATILE"
    return "SHAREHOLDER_COUNT_RISING" if positive else "SHAREHOLDER_COUNT_FALLING"


def normalize_shareholder_count(payload, source_url, provider_code, fetched_at):
    raw_rows, raw_section = _rows(payload)
    history, dropped = _history(raw_rows)
    _attach_period_changes(history)
    latest = next((row for row in history if row.get("shareholder_count") is not None), None)
    windows = {f"{months}m": _window(history, months) for months in (3, 6, 12)}
    flags = []
    if raw_section is None:
        flags.append("SHAREHOLDER_COUNT_SECTION_NOT_EXPOSED")
    if dropped:
        flags.append("DROPPED_UNDATED_PROVIDER_ROWS")
    if latest is None:
        flags.append("NO_DATED_SHAREHOLDER_COUNT")
    if latest and len([row for row in history if row.get("shareholder_count") is not None]) < 2:
        flags.append("INSUFFICIENT_HISTORY_FOR_PERIOD_CHANGE")
    if latest and all(item is None for item in windows.values()):
        flags.append("INSUFFICIENT_HISTORY_FOR_3_6_12M_TRENDS")
    gap = ((latest or {}).get("change_from_previous") or {}).get("provider_delta_gap_pp")
    if gap is not None and abs(gap) > 0.1:
        flags.append("PROVIDER_PERIOD_CHANGE_MISMATCH")
    status = "UNAVAILABLE" if latest is None else "PARTIAL" if flags else "OK"
    return {
        "status": status,
        "as_of_date": latest["report_date"] if latest else None,
        "latest": latest,
        "history": history,
        "window_trends": windows,
        "trend": _trend(windows),
        "metadata": {
            "freshness": "DISCLOSED_SHAREHOLDER_COUNT_HISTORY",
            "realtime": False,
            "disclosure_lag": True,
            "quality": "PASS" if status == "OK" else "PARTIAL" if status == "PARTIAL" else "FAILED",
            "quality_flags": flags,
            "history_period_limit": HISTORY_LIMIT,
            "date_semantics": "REPORT_OR_DISCLOSURE_PERIOD_DATE; NOT_REALTIME",
            "trend_policy": "3/6/12m on-or-before baselines; +/-1% stable band; mixed material signs volatile",
            "average_holding_policy": "PROVIDER_DECLARED_ONLY; NO_CROSS_DATE_SHARE_COUNT_INFERENCE",
        },
        "provenance": {
            "provider": "Eastmoney",
            "source_tier": "PRIMARY_PROVIDER",
            "endpoint": core.SHAREHOLDER_RESEARCH_ENDPOINT,
            "source_url": source_url,
            "provider_code": provider_code,
            "fetched_at": fetched_at,
            "raw_section": raw_section,
            "field_mapping": {
                "shareholder_count": "HOLDER_TOTAL_NUM",
                "period_change_percent": "TOTAL_NUM_RATIO",
                "average_free_shares": "AVG_FREE_SHARES",
                "average_holding_amount": "AVG_HOLD_AMT",
            },
            "derived_fields": {
                "change_from_previous": "adjacent disclosed counts",
                "window_trends": "latest vs on-or-before 3/6/12 month baselines",
                "trend": "deterministic structural direction only",
            },
        },
    }


def _deferred(fetched_at):
    return {
        "status": "DEFERRED", "as_of_date": None, "latest": None, "history": [],
        "window_trends": {"3m": None, "6m": None, "12m": None}, "trend": "UNKNOWN",
        "metadata": {
            "freshness": "NOT_FETCHED_IN_INTRADAY_FAST", "realtime": False,
            "disclosure_lag": True, "quality": "PARTIAL",
            "quality_flags": ["FULL_ONLY_SHAREHOLDER_COUNT_SLICE"], "history_period_limit": HISTORY_LIMIT,
        },
        "provenance": {
            "provider": "Eastmoney", "source_tier": "PRIMARY_PROVIDER",
            "endpoint": core.SHAREHOLDER_RESEARCH_ENDPOINT, "source_url": None,
            "provider_code": None, "fetched_at": fetched_at, "raw_section": None,
        },
    }


def _status(context):
    names = ("share_structure", "controllers", "top_holders", "institutional_holdings", "shareholder_count")
    statuses = [(context.get(name) or {}).get("status") for name in names]
    if statuses and all(value == "DEFERRED" for value in statuses):
        return "DEFERRED"
    if statuses and all(value == "OK" for value in statuses):
        return "OK"
    if any(value in ("OK", "PARTIAL") for value in statuses):
        return "PARTIAL"
    return "UNAVAILABLE"


def _fetch_one(base, code, fetched_at):
    provider_code = core._provider_code(base, code)
    try:
        payload, url, provider_code = fetch_shareholder_count(base, code)
        return code, normalize_shareholder_count(payload, url, provider_code, fetched_at)
    except Exception as exc:
        value = _deferred(fetched_at)
        value["status"] = "UNAVAILABLE"
        value["metadata"].update(
            freshness="UNAVAILABLE", quality="FAILED", quality_flags=["PROVIDER_ERROR"],
            error=f"{type(exc).__name__}: {exc}",
        )
        value["provenance"]["provider_code"] = provider_code
        return code, value


def extend_snapshot(snapshot_path, base, execution_mode):
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    fetched_at = core._runner_time_iso(snapshot)
    detail = snapshot.get("detail_stocks") or {}
    results = {}
    if str(execution_mode or "").upper() != "FULL":
        results = {code: _deferred(fetched_at) for code in detail}
    else:
        for code in detail:
            key, value = _fetch_one(base, code, fetched_at)
            results[key] = value
    for code, value in results.items():
        context = detail[code].setdefault("ownership_and_capital", {})
        context["shareholder_count"] = value
        context["status"] = _status(context)
        latest = value.get("latest") or {}
        print(
            f"OWNERSHIP_SHAREHOLDER_COUNT {code} status={value.get('status')} "
            f"as_of={value.get('as_of_date')} count={latest.get('shareholder_count')} trend={value.get('trend')}",
            flush=True,
        )
    summary = snapshot.setdefault("ownership_and_capital_summary", {})
    implemented = list(summary.get("implemented_sections") or [])
    if "shareholder_count" not in implemented:
        implemented.append("shareholder_count")
    summary["implemented_sections"] = implemented
    summary["shareholder_count_contract"] = (
        "DISCLOSED_HISTORY<=12; PERIOD_DELTA; 3_6_12M_WINDOWS; DISCLOSURE_LAG_EXPLICIT"
    )
    status_by_code = {
        code: detail[code]["ownership_and_capital"].get("status")
        for code in sorted(detail) if "ownership_and_capital" in detail[code]
    }
    summary["status_by_code"] = status_by_code
    summary["status"] = (
        "OK" if status_by_code and all(v == "OK" for v in status_by_code.values())
        else "DEFERRED" if status_by_code and all(v == "DEFERRED" for v in status_by_code.values())
        else "PARTIAL" if status_by_code else "UNAVAILABLE"
    )
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
