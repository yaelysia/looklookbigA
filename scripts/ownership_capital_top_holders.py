import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import ownership_capital_base as core


DATA_CENTER_ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
TOP_HOLDER_HISTORY_PERIODS = 4


def _fetch_datacenter_rows(base, code, report_name, rank_column):
    params = {
        "sortColumns": f"END_DATE,{rank_column}",
        "sortTypes": "-1,1",
        "pageSize": str(TOP_HOLDER_HISTORY_PERIODS * 10),
        "pageNumber": "1",
        "reportName": report_name,
        "columns": "ALL",
        "source": "WEB",
        "client": "WEB",
        "filter": f'(SECURITY_CODE="{code}")',
    }
    url = DATA_CENTER_ENDPOINT + "?" + urllib.parse.urlencode(params)
    payload = json.loads(base.http_get(url))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Eastmoney response is not an object: {report_name}")
    result = payload.get("result") or {}
    rows = (result.get("data") or []) if isinstance(result, dict) else []
    if not isinstance(rows, list):
        raise RuntimeError(f"Eastmoney result.data is not a list: {report_name}")
    return rows, url


def fetch_top_holders(base, code):
    provider_code = core._provider_code(base, code)
    total_rows, total_url = _fetch_datacenter_rows(base, code, "RPT_DMSK_HOLDERS", "RANK")
    float_rows, float_url = _fetch_datacenter_rows(
        base,
        code,
        "RPT_F10_EH_FREEHOLDERS",
        "HOLDER_RANK",
    )
    return total_rows, float_rows, total_url, float_url, provider_code


def _holder_ratio(row, scope):
    if scope == "TOTAL_SHARES":
        value = row.get("HOLD_NUM_RATIO")
        if value in (None, ""):
            value = row.get("HOLD_RATIO")
        return core._as_float(value)
    value = row.get("FREE_HOLDNUM_RATIO")
    if value in (None, ""):
        value = row.get("HOLD_RATIO")
    return core._as_float(value)


def _normalize_holder_history(rows, scope):
    grouped = {}
    if not isinstance(rows, list):
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        period = core._date(row.get("END_DATE") or row.get("REPORT_DATE"))
        name = str(row.get("HOLDER_NAME") or "").strip()
        if not period or not name:
            continue
        rank = _as_int(row.get("HOLDER_RANK") or row.get("RANK"))
        change_shares_raw = row.get("XZCHANGE")
        if change_shares_raw in (None, ""):
            change_shares_raw = row.get("HOLD_NUM_CHANGE")
        holder = {
            "rank": rank,
            "name": name,
            "hold_shares": core._as_float(row.get("HOLD_NUM")),
            "hold_ratio_percent": _holder_ratio(row, scope),
            "change_shares": core._as_float(change_shares_raw),
            "change_ratio_percent": core._as_float(row.get("CHANGE_RATIO")),
            "hold_ratio_change_percent": core._as_float(row.get("HOLD_RATIO_CHANGE")),
            "change_state": row.get("HOLDNUM_CHANGE_NAME") or row.get("HOLD_CHANGE") or None,
            "holder_type": row.get("HOLDER_NEWTYPE") or row.get("HOLDER_TYPE") or None,
            "is_hold_org_raw": row.get("IS_HOLDORG"),
            "update_date": core._date(row.get("UPDATE_DATE")),
        }
        grouped.setdefault(period, []).append(holder)

    history = []
    for period in sorted(grouped, reverse=True)[:TOP_HOLDER_HISTORY_PERIODS]:
        holders = sorted(
            grouped[period],
            key=lambda item: (
                item.get("rank") is None,
                item.get("rank") or 999,
                item.get("name") or "",
            ),
        )[:10]
        ratios = [holder.get("hold_ratio_percent") for holder in holders]
        complete_ratios = bool(holders) and all(value is not None for value in ratios)
        quality_flags = []
        if not holders:
            quality_flags.append("NO_HOLDER_ROWS")
        if holders and not complete_ratios:
            quality_flags.append("MISSING_HOLDER_RATIO")
        if len(holders) < 10:
            quality_flags.append("FEWER_THAN_10_REPORTED_HOLDERS")
        history.append(
            {
                "report_date": period,
                "scope": scope,
                "holder_count": len(holders),
                "holders": holders,
                "concentration_percent": (
                    core._round(sum(ratios), 4) if complete_ratios else None
                ),
                "status": "OK" if complete_ratios else "PARTIAL" if holders else "UNAVAILABLE",
                "quality_flags": quality_flags,
            }
        )
    return history


def _as_int(value):
    try:
        if value in (None, "", "-"):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def normalize_top_holders(
    total_rows,
    float_rows,
    total_source_url,
    float_source_url,
    provider_code,
    fetched_at,
):
    total_history = _normalize_holder_history(total_rows, "TOTAL_SHARES")
    float_history = _normalize_holder_history(float_rows, "FLOAT_SHARES")
    total_latest = total_history[0] if total_history else None
    float_latest = float_history[0] if float_history else None

    quality_flags = []
    if not total_history:
        quality_flags.append("TOP_SHAREHOLDERS_UNAVAILABLE")
    if not float_history:
        quality_flags.append("TOP_FLOAT_SHAREHOLDERS_UNAVAILABLE")
    if total_latest and total_latest.get("status") != "OK":
        quality_flags.append("LATEST_TOP_SHAREHOLDERS_PARTIAL")
    if float_latest and float_latest.get("status") != "OK":
        quality_flags.append("LATEST_TOP_FLOAT_SHAREHOLDERS_PARTIAL")

    if total_history and float_history:
        status = "OK" if not quality_flags else "PARTIAL"
    elif total_history or float_history:
        status = "PARTIAL"
    else:
        status = "UNAVAILABLE"
    dates = [
        value.get("report_date")
        for value in (total_latest, float_latest)
        if value and value.get("report_date")
    ]
    return {
        "status": status,
        "as_of_date": max(dates) if dates else None,
        "top_shareholders": {
            "status": total_latest.get("status") if total_latest else "UNAVAILABLE",
            "latest": total_latest,
            "history": total_history,
        },
        "top_float_shareholders": {
            "status": float_latest.get("status") if float_latest else "UNAVAILABLE",
            "latest": float_latest,
            "history": float_history,
        },
        "top10_concentration_percent": (
            total_latest.get("concentration_percent") if total_latest else None
        ),
        "float_top10_concentration_percent": (
            float_latest.get("concentration_percent") if float_latest else None
        ),
        "metadata": {
            "freshness": "REPORT_PERIOD_HISTORY",
            "realtime": False,
            "quality": "PASS" if status == "OK" else "PARTIAL" if status == "PARTIAL" else "FAILED",
            "quality_flags": quality_flags,
            "history_period_limit": TOP_HOLDER_HISTORY_PERIODS,
            "date_semantics": "END_DATE_REPORT_PERIOD; DISCLOSED_DATA_MAY_LAG_MARKET_TIME",
            "holder_type_policy": "PROVIDER_DECLARED_ONLY; NO_NAME_BASED_CLASSIFICATION",
        },
        "provenance": {
            "provider": "Eastmoney",
            "source_tier": "PRIMARY_PROVIDER",
            "endpoint": DATA_CENTER_ENDPOINT,
            "provider_code": provider_code,
            "fetched_at": fetched_at,
            "reports": {
                "top_shareholders": {
                    "report_name": "RPT_DMSK_HOLDERS",
                    "source_url": total_source_url,
                    "ratio_field": "HOLD_NUM_RATIO fallback HOLD_RATIO",
                },
                "top_float_shareholders": {
                    "report_name": "RPT_F10_EH_FREEHOLDERS",
                    "source_url": float_source_url,
                    "ratio_field": "FREE_HOLDNUM_RATIO fallback HOLD_RATIO",
                },
            },
            "field_mapping": {
                "report_date": "END_DATE",
                "rank": "HOLDER_RANK or RANK",
                "holder_name": "HOLDER_NAME",
                "hold_shares": "HOLD_NUM",
                "change_shares": "XZCHANGE fallback HOLD_NUM_CHANGE",
                "holder_type": "HOLDER_NEWTYPE fallback HOLDER_TYPE",
            },
        },
    }


def _deferred_top_holders(fetched_at):
    return {
        "status": "DEFERRED",
        "as_of_date": None,
        "top_shareholders": {"status": "DEFERRED", "latest": None, "history": []},
        "top_float_shareholders": {"status": "DEFERRED", "latest": None, "history": []},
        "top10_concentration_percent": None,
        "float_top10_concentration_percent": None,
        "metadata": {
            "freshness": "NOT_FETCHED_IN_INTRADAY_FAST",
            "realtime": False,
            "quality": "PARTIAL",
            "quality_flags": ["FULL_ONLY_TOP_HOLDER_HISTORY_SLICE"],
            "history_period_limit": TOP_HOLDER_HISTORY_PERIODS,
        },
        "provenance": {
            "provider": "Eastmoney",
            "source_tier": "PRIMARY_PROVIDER",
            "endpoint": DATA_CENTER_ENDPOINT,
            "provider_code": None,
            "fetched_at": fetched_at,
        },
    }


def _unavailable_top_holders(fetched_at, error):
    value = _deferred_top_holders(fetched_at)
    value["status"] = "UNAVAILABLE"
    value["top_shareholders"]["status"] = "UNAVAILABLE"
    value["top_float_shareholders"]["status"] = "UNAVAILABLE"
    value["metadata"] = {
        "freshness": "UNAVAILABLE",
        "realtime": False,
        "quality": "FAILED",
        "quality_flags": ["PROVIDER_ERROR"],
        "history_period_limit": TOP_HOLDER_HISTORY_PERIODS,
        "error": error,
    }
    return value


def _context_status(context):
    sections = [
        context.get("share_structure") or {},
        context.get("controllers") or {},
        context.get("top_holders") or {},
    ]
    statuses = [section.get("status") for section in sections]
    if statuses and all(status == "DEFERRED" for status in statuses):
        return "DEFERRED"
    if statuses and all(status == "OK" for status in statuses):
        return "OK"
    if any(status in ("OK", "PARTIAL") for status in statuses):
        return "PARTIAL"
    return "UNAVAILABLE"


def _fetch_one(base, code, fetched_at):
    try:
        total_rows, float_rows, total_url, float_url, provider_code = fetch_top_holders(base, code)
        return code, normalize_top_holders(
            total_rows,
            float_rows,
            total_url,
            float_url,
            provider_code,
            fetched_at,
        )
    except Exception as exc:
        return code, _unavailable_top_holders(
            fetched_at,
            f"{type(exc).__name__}: {exc}",
        )


def extend_snapshot(snapshot_path, base, execution_mode):
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    fetched_at = core._runner_time_iso(snapshot)
    detail = snapshot.get("detail_stocks") or {}
    top_results = {}

    if str(execution_mode or "").upper() != "FULL":
        for code in detail:
            top_results[code] = _deferred_top_holders(fetched_at)
    elif detail:
        with ThreadPoolExecutor(max_workers=max(1, min(2, len(detail)))) as pool:
            futures = [pool.submit(_fetch_one, base, code, fetched_at) for code in detail]
            for future in as_completed(futures):
                code, value = future.result()
                top_results[code] = value

    for code, top_holders in top_results.items():
        context = detail[code].setdefault("ownership_and_capital", {})
        context["top_holders"] = top_holders
        context["status"] = _context_status(context)
        print(
            "OWNERSHIP_TOP_HOLDERS "
            f"{code} status={top_holders.get('status')} "
            f"as_of={top_holders.get('as_of_date')} "
            f"top10={top_holders.get('top10_concentration_percent')} "
            f"float_top10={top_holders.get('float_top10_concentration_percent')}",
            flush=True,
        )

    summary = snapshot.setdefault("ownership_and_capital_summary", {})
    implemented = list(summary.get("implemented_sections") or [])
    if "top_holders" not in implemented:
        implemented.append("top_holders")
    summary["implemented_sections"] = implemented
    summary["top_holders_contract"] = (
        "REPORT_PERIOD_HISTORY<=4; TOTAL=RPT_DMSK_HOLDERS; "
        "FLOAT=RPT_F10_EH_FREEHOLDERS; HOLDER_TYPE=PROVIDER_DECLARED_ONLY"
    )
    status_by_code = {
        code: (detail[code].get("ownership_and_capital") or {}).get("status")
        for code in sorted(detail)
        if "ownership_and_capital" in detail[code]
    }
    summary["status_by_code"] = status_by_code
    summary["status"] = (
        "OK"
        if status_by_code and all(value == "OK" for value in status_by_code.values())
        else "DEFERRED"
        if status_by_code and all(value == "DEFERRED" for value in status_by_code.values())
        else "PARTIAL"
        if status_by_code
        else "UNAVAILABLE"
    )
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
