import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import ownership_capital_base as core


INSTITUTIONAL_DETAIL_ENDPOINT = "https://data.eastmoney.com/dataapi/zlsj/detail"
INSTITUTIONAL_HISTORY_PERIODS = 4
INSTITUTIONAL_PAGE_SIZE = 500


def _first(row, *names):
    for name in names:
        value = row.get(name)
        if value not in (None, "", "-"):
            return value
    return None


def _as_int(value):
    try:
        if value in (None, "", "-"):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _complete_sum(values):
    values = list(values)
    if not values:
        return 0.0
    if any(value is None for value in values):
        return None
    return core._round(sum(values), 4)


def _fetch_period(base, code, report_date):
    params = {
        "SHType": "0",
        "SHCode": "",
        "SCode": code,
        "ReportDate": report_date,
        "sortField": "HOLDER_CODE",
        "sortDirec": "1",
        "pageSize": str(INSTITUTIONAL_PAGE_SIZE),
        "pageNum": "1",
    }
    url = INSTITUTIONAL_DETAIL_ENDPOINT + "?" + urllib.parse.urlencode(params)
    payload = json.loads(base.http_get(url))
    if not isinstance(payload, dict):
        raise RuntimeError("Eastmoney institutional detail response is not an object")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise RuntimeError("Eastmoney institutional detail data is not a list")

    page_count = _as_int(
        payload.get("pages")
        or payload.get("pageCount")
        or payload.get("page_count")
    )
    total_count = _as_int(
        payload.get("count")
        or payload.get("total")
        or payload.get("totalCount")
    )
    return {
        "report_date": report_date,
        "rows": rows,
        "source_url": url,
        "page_count": page_count,
        "provider_total_count": total_count,
        "error": None,
    }


def fetch_institutional_holdings(base, code, report_dates):
    results = []
    for report_date in list(report_dates)[:INSTITUTIONAL_HISTORY_PERIODS]:
        try:
            results.append(_fetch_period(base, code, report_date))
        except Exception as exc:
            results.append(
                {
                    "report_date": report_date,
                    "rows": [],
                    "source_url": None,
                    "page_count": None,
                    "provider_total_count": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return results


def _normalized_institution(row):
    if not isinstance(row, dict):
        return None
    name = str(_first(row, "HOLDER_NAME", "ORG_NAME", "SHAREHOLDER_NAME") or "").strip()
    holder_code = str(_first(row, "HOLDER_CODE", "ORG_CODE") or "").strip() or None
    if not name and not holder_code:
        return None
    provider_type = _first(
        row,
        "HOLDER_TYPE",
        "HOLDER_NEWTYPE",
        "ORG_TYPE_NAME",
        "ORG_TYPE",
        "SH_TYPE_NAME",
    )
    provider_type = str(provider_type).strip() if provider_type not in (None, "") else None
    return {
        "holder_code": holder_code,
        "holder_name": name or None,
        "provider_type": provider_type,
        "hold_shares": core._as_float(_first(row, "HOLD_NUM", "HOLD_SHARES")),
        "hold_market_value": core._as_float(_first(row, "HOLD_MARKET_CAP", "HOLD_MARKET_VALUE")),
        "total_shares_ratio_percent": core._as_float(
            _first(row, "TOTAL_SHARES_RATIO", "HOLD_NUM_RATIO", "HOLD_RATIO")
        ),
        "float_shares_ratio_percent": core._as_float(
            _first(row, "FREE_SHARES_RATIO", "FREE_HOLDNUM_RATIO")
        ),
        "hold_shares_change": core._as_float(
            _first(row, "HOLD_NUM_CHANGE", "HOLDCHANGE", "XZCHANGE")
        ),
        "hold_ratio_change_percent": core._as_float(_first(row, "HOLD_RATIO_CHANGE")),
        "previous_hold_shares": core._as_float(_first(row, "PRE_HOLD_NUM", "PRE_HOLD_SHARES")),
        "announcement_date": core._date(
            _first(row, "NOTICE_DATE", "ANNOUNCE_DATE", "UPDATE_DATE")
        ),
    }


def _type_breakdown(institutions):
    grouped = {}
    for item in institutions:
        key = item.get("provider_type") or "UNCLASSIFIED"
        grouped.setdefault(key, []).append(item)

    result = []
    for provider_type in sorted(grouped):
        rows = grouped[provider_type]
        result.append(
            {
                "provider_type": provider_type,
                "institution_count": len(rows),
                "hold_shares": _complete_sum(item.get("hold_shares") for item in rows),
                "total_shares_ratio_percent": _complete_sum(
                    item.get("total_shares_ratio_percent") for item in rows
                ),
                "float_shares_ratio_percent": _complete_sum(
                    item.get("float_shares_ratio_percent") for item in rows
                ),
            }
        )
    return result


def _fund_ratio_from_declared_types(type_breakdown):
    fund_rows = [
        item
        for item in type_breakdown
        if item.get("provider_type")
        and (
            "基金" in item["provider_type"]
            or item["provider_type"].strip().upper() == "FUND"
        )
    ]
    if not fund_rows:
        return None
    values = [item.get("total_shares_ratio_percent") for item in fund_rows]
    return _complete_sum(values)


def _normalize_period(raw):
    report_date = raw.get("report_date")
    if raw.get("error"):
        return {
            "report_date": report_date,
            "status": "UNAVAILABLE",
            "institution_count": None,
            "hold_shares": None,
            "hold_market_value": None,
            "hold_ratio_percent": None,
            "float_hold_ratio_percent": None,
            "fund_hold_ratio_percent": None,
            "provider_type_breakdown": [],
            "institutions": [],
            "change_from_previous": None,
            "quality_flags": ["PROVIDER_ERROR"],
            "error": raw.get("error"),
            "source_url": raw.get("source_url"),
        }

    institutions = []
    dropped = 0
    for row in raw.get("rows") or []:
        value = _normalized_institution(row)
        if value is None:
            dropped += 1
            continue
        institutions.append(value)

    hold_shares = _complete_sum(item.get("hold_shares") for item in institutions)
    hold_market_value = _complete_sum(item.get("hold_market_value") for item in institutions)
    hold_ratio = _complete_sum(item.get("total_shares_ratio_percent") for item in institutions)
    float_ratio = _complete_sum(item.get("float_shares_ratio_percent") for item in institutions)
    type_breakdown = _type_breakdown(institutions)
    fund_ratio = _fund_ratio_from_declared_types(type_breakdown)

    quality_flags = []
    if not institutions:
        quality_flags.append("NO_DISCLOSED_INSTITUTION_ROWS")
    if dropped:
        quality_flags.append("DROPPED_UNIDENTIFIED_PROVIDER_ROWS")
    if institutions and hold_shares is None:
        quality_flags.append("INCOMPLETE_HOLD_SHARES")
    if institutions and hold_ratio is None:
        quality_flags.append("INCOMPLETE_TOTAL_SHARE_RATIO")
    if institutions and float_ratio is None:
        quality_flags.append("INCOMPLETE_FLOAT_SHARE_RATIO")
    if institutions and fund_ratio is None:
        quality_flags.append("FUND_RATIO_UNAVAILABLE_FROM_PROVIDER_TYPE")

    page_count = raw.get("page_count")
    provider_total = raw.get("provider_total_count")
    if page_count is not None and page_count > 1:
        quality_flags.append("PROVIDER_PAGINATION_TRUNCATED")
    elif provider_total is not None and provider_total > len(raw.get("rows") or []):
        quality_flags.append("PROVIDER_PAGINATION_TRUNCATED")

    material_partial = any(
        flag
        in {
            "DROPPED_UNIDENTIFIED_PROVIDER_ROWS",
            "INCOMPLETE_HOLD_SHARES",
            "INCOMPLETE_TOTAL_SHARE_RATIO",
            "PROVIDER_PAGINATION_TRUNCATED",
        }
        for flag in quality_flags
    )
    return {
        "report_date": report_date,
        "status": "PARTIAL" if material_partial else "OK",
        "institution_count": len(institutions),
        "hold_shares": hold_shares,
        "hold_market_value": hold_market_value,
        "hold_ratio_percent": hold_ratio,
        "float_hold_ratio_percent": float_ratio,
        "fund_hold_ratio_percent": fund_ratio,
        "provider_type_breakdown": type_breakdown,
        "institutions": institutions,
        "change_from_previous": None,
        "quality_flags": quality_flags,
        "source_url": raw.get("source_url"),
    }


def _attach_period_changes(history):
    for index, period in enumerate(history):
        if index + 1 >= len(history):
            period["change_from_previous"] = None
            continue
        previous = history[index + 1]
        if period.get("status") == "UNAVAILABLE" or previous.get("status") == "UNAVAILABLE":
            period["change_from_previous"] = None
            continue

        def delta(field):
            current = period.get(field)
            prior = previous.get(field)
            if current is None or prior is None:
                return None
            return core._round(current - prior, 4)

        period["change_from_previous"] = {
            "previous_report_date": previous.get("report_date"),
            "institution_count_delta": (
                period.get("institution_count") - previous.get("institution_count")
                if period.get("institution_count") is not None
                and previous.get("institution_count") is not None
                else None
            ),
            "hold_shares_delta": delta("hold_shares"),
            "hold_ratio_change_pp": delta("hold_ratio_percent"),
            "float_hold_ratio_change_pp": delta("float_hold_ratio_percent"),
            "fund_hold_ratio_change_pp": delta("fund_hold_ratio_percent"),
        }


def normalize_institutional_holdings(raw_periods, provider_code, fetched_at):
    history = [_normalize_period(item) for item in raw_periods]
    history = sorted(
        history,
        key=lambda item: item.get("report_date") or "",
        reverse=True,
    )[:INSTITUTIONAL_HISTORY_PERIODS]
    _attach_period_changes(history)
    latest = next((item for item in history if item.get("status") != "UNAVAILABLE"), None)

    quality_flags = []
    if not history:
        quality_flags.append("NO_DISCLOSED_REPORT_PERIODS")
    if history and any(item.get("status") == "UNAVAILABLE" for item in history):
        quality_flags.append("SOME_REPORT_PERIODS_UNAVAILABLE")
    if latest and latest.get("status") == "PARTIAL":
        quality_flags.append("LATEST_PERIOD_PARTIAL")
    if latest and latest.get("fund_hold_ratio_percent") is None:
        quality_flags.append("LATEST_FUND_RATIO_UNAVAILABLE_FROM_PROVIDER_TYPE")

    if latest is None:
        status = "UNAVAILABLE"
    elif quality_flags or any(item.get("status") == "PARTIAL" for item in history):
        status = "PARTIAL"
    else:
        status = "OK"

    return {
        "status": status,
        "as_of_date": latest.get("report_date") if latest else None,
        "latest": latest,
        "history": history,
        "metadata": {
            "freshness": "REPORT_PERIOD_HISTORY",
            "realtime": False,
            "disclosure_lag": True,
            "quality": (
                "PASS" if status == "OK" else "PARTIAL" if status == "PARTIAL" else "FAILED"
            ),
            "quality_flags": quality_flags,
            "history_period_limit": INSTITUTIONAL_HISTORY_PERIODS,
            "date_semantics": "REPORT_DATE; DISCLOSED_DATA_MAY_LAG_MARKET_TIME",
            "report_period_source": "TOP_HOLDERS_DISCLOSED_REPORT_PERIODS",
            "provider_type_policy": "PROVIDER_DECLARED_ONLY; NO_NAME_BASED_CLASSIFICATION",
            "fund_disclosure_caveat": (
                "Q1/Q3 fund holdings may be incomplete; H1/FY disclosures are more complete"
            ),
        },
        "provenance": {
            "provider": "Eastmoney",
            "source_tier": "PRIMARY_PROVIDER",
            "endpoint": INSTITUTIONAL_DETAIL_ENDPOINT,
            "provider_code": provider_code,
            "fetched_at": fetched_at,
            "query_contract": (
                "SHType=0; SCode=<code>; ReportDate=<disclosed report date>; "
                "sortField=HOLDER_CODE; pageNum=1"
            ),
            "period_sources": [
                {
                    "report_date": item.get("report_date"),
                    "source_url": item.get("source_url"),
                }
                for item in history
            ],
            "field_mapping": {
                "institution_identity": "HOLDER_CODE/HOLDER_NAME (fallback ORG_CODE/ORG_NAME)",
                "provider_type": (
                    "HOLDER_TYPE/HOLDER_NEWTYPE/ORG_TYPE_NAME/ORG_TYPE/SH_TYPE_NAME"
                ),
                "hold_shares": "HOLD_NUM fallback HOLD_SHARES",
                "hold_market_value": "HOLD_MARKET_CAP fallback HOLD_MARKET_VALUE",
                "hold_ratio_percent": (
                    "TOTAL_SHARES_RATIO fallback HOLD_NUM_RATIO/HOLD_RATIO"
                ),
                "float_hold_ratio_percent": "FREE_SHARES_RATIO fallback FREE_HOLDNUM_RATIO",
                "hold_shares_change": "HOLD_NUM_CHANGE fallback HOLDCHANGE/XZCHANGE",
                "hold_ratio_change_percent": "HOLD_RATIO_CHANGE",
            },
            "derived_fields": {
                "period_totals": "sum only when every normalized institution row exposes the field",
                "period_changes": "latest aggregate minus immediately previous disclosed report period",
                "fund_hold_ratio_percent": (
                    "sum total-share ratio only for provider-declared FUND/基金 types"
                ),
            },
        },
    }


def _deferred_institutional(fetched_at):
    return {
        "status": "DEFERRED",
        "as_of_date": None,
        "latest": None,
        "history": [],
        "metadata": {
            "freshness": "NOT_FETCHED_IN_INTRADAY_FAST",
            "realtime": False,
            "disclosure_lag": True,
            "quality": "PARTIAL",
            "quality_flags": ["FULL_ONLY_INSTITUTIONAL_HOLDINGS_SLICE"],
            "history_period_limit": INSTITUTIONAL_HISTORY_PERIODS,
        },
        "provenance": {
            "provider": "Eastmoney",
            "source_tier": "PRIMARY_PROVIDER",
            "endpoint": INSTITUTIONAL_DETAIL_ENDPOINT,
            "provider_code": None,
            "fetched_at": fetched_at,
        },
    }


def _unavailable_institutional(fetched_at, provider_code, reason):
    value = _deferred_institutional(fetched_at)
    value["status"] = "UNAVAILABLE"
    value["metadata"] = {
        "freshness": "UNAVAILABLE",
        "realtime": False,
        "disclosure_lag": True,
        "quality": "FAILED",
        "quality_flags": [reason],
        "history_period_limit": INSTITUTIONAL_HISTORY_PERIODS,
    }
    value["provenance"]["provider_code"] = provider_code
    return value


def _report_dates_from_context(context):
    top_holders = context.get("top_holders") or {}
    dates = []
    for section_name in ("top_shareholders", "top_float_shareholders"):
        section = top_holders.get(section_name) or {}
        for period in section.get("history") or []:
            report_date = core._date(period.get("report_date"))
            if report_date and report_date not in dates:
                dates.append(report_date)
    return sorted(dates, reverse=True)[:INSTITUTIONAL_HISTORY_PERIODS]


def _context_status(context):
    sections = [
        context.get("share_structure") or {},
        context.get("controllers") or {},
        context.get("top_holders") or {},
        context.get("institutional_holdings") or {},
    ]
    statuses = [section.get("status") for section in sections]
    if statuses and all(status == "DEFERRED" for status in statuses):
        return "DEFERRED"
    if statuses and all(status == "OK" for status in statuses):
        return "OK"
    if any(status in ("OK", "PARTIAL") for status in statuses):
        return "PARTIAL"
    return "UNAVAILABLE"


def _fetch_one(base, code, report_dates, fetched_at):
    provider_code = core._provider_code(base, code)
    if not report_dates:
        return code, _unavailable_institutional(
            fetched_at,
            provider_code,
            "NO_DISCLOSED_REPORT_PERIODS_FROM_TOP_HOLDERS",
        )
    raw_periods = fetch_institutional_holdings(base, code, report_dates)
    return code, normalize_institutional_holdings(raw_periods, provider_code, fetched_at)


def extend_snapshot(snapshot_path, base, execution_mode):
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    fetched_at = core._runner_time_iso(snapshot)
    detail = snapshot.get("detail_stocks") or {}
    results = {}

    if str(execution_mode or "").upper() != "FULL":
        for code in detail:
            results[code] = _deferred_institutional(fetched_at)
    elif detail:
        with ThreadPoolExecutor(max_workers=max(1, min(2, len(detail)))) as pool:
            futures = []
            for code, stock in detail.items():
                context = stock.get("ownership_and_capital") or {}
                report_dates = _report_dates_from_context(context)
                futures.append(pool.submit(_fetch_one, base, code, report_dates, fetched_at))
            for future in as_completed(futures):
                code, value = future.result()
                results[code] = value

    for code, institutional in results.items():
        context = detail[code].setdefault("ownership_and_capital", {})
        context["institutional_holdings"] = institutional
        context["status"] = _context_status(context)
        latest = institutional.get("latest") or {}
        print(
            "OWNERSHIP_INSTITUTIONAL "
            f"{code} status={institutional.get('status')} "
            f"as_of={institutional.get('as_of_date')} "
            f"institutions={latest.get('institution_count')} "
            f"hold_ratio={latest.get('hold_ratio_percent')}",
            flush=True,
        )

    summary = snapshot.setdefault("ownership_and_capital_summary", {})
    implemented = list(summary.get("implemented_sections") or [])
    if "institutional_holdings" not in implemented:
        implemented.append("institutional_holdings")
    summary["implemented_sections"] = implemented
    summary["institutional_holdings_contract"] = (
        "REPORT_PERIOD_HISTORY<=4; REPORT_DATES_FROM_TOP_HOLDERS; "
        "PROVIDER_TYPE_ONLY; DISCLOSURE_LAG_EXPLICIT"
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
