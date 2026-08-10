import json
import os
import statistics
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import data_metadata


FUNDAMENTALS_VERSION = "v1"
CACHE_SCHEMA = 1
REPORTS = {
    "main": ("RPT_LICO_FN_CPD", "REPORTDATE"),
    "income": ("RPT_DMSK_FN_INCOME", "REPORT_DATE"),
    "balance": ("RPT_DMSK_FN_BALANCE", "REPORT_DATE"),
    "cashflow": ("RPT_DMSK_FN_CASHFLOW", "REPORT_DATE"),
}
PERIOD_ORDER = {"Q1": 1, "H1": 2, "Q3": 3, "FY": 4}


def _as_float(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value, digits=4):
    return None if value is None else round(float(value), digits)


def _get(row, *names):
    row = row or {}
    for name in names:
        value = row.get(name)
        if value not in (None, "", "-"):
            return value
    return None


def _date(value):
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else None


def _iso(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.isoformat(timespec="seconds")
    except ValueError:
        if len(text) >= 10:
            return text[:10] + "T00:00:00+08:00"
    return None


def _pct(current, previous):
    current = _as_float(current)
    previous = _as_float(previous)
    if current is None or previous in (None, 0):
        return None
    return (current / previous - 1.0) * 100.0


def _ratio(a, b):
    a = _as_float(a)
    b = _as_float(b)
    if a is None or b in (None, 0):
        return None
    return a / b


def _sub(a, b):
    a = _as_float(a)
    b = _as_float(b)
    if a is None or b is None:
        return None
    return a - b


def _cache_path(code):
    root = Path(os.environ.get("MARKET_HISTORY_DIR", ".market-data/history"))
    return root / "fundamentals" / f"{code}.json"


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch_report(base, code, report_name, sort_column, page_size=40):
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{code}")',
        "sortColumns": sort_column,
        "sortTypes": "-1",
        "pageNumber": "1",
        "pageSize": str(page_size),
        "source": "WEB",
        "client": "WEB",
        "_": str(int(time.time() * 1000)),
    }
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
    obj = json.loads(base.http_get(url))
    result = (obj or {}).get("result") or {}
    rows = result.get("data") or []
    return [x for x in rows if isinstance(x, dict)], url


def fetch_all_reports(base, code):
    out = {}
    urls = {}
    errors = []
    # Four independent low-frequency reports are fetched concurrently only in
    # FULL. FAST never calls this function.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(_fetch_report, base, code, report_name, sort_column): key
            for key, (report_name, sort_column) in REPORTS.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                rows, url = future.result()
                out[key] = rows
                urls[key] = url
            except Exception as exc:
                out[key] = []
                errors.append(f"{key}: {type(exc).__name__}: {exc}")
    return out, urls, errors


def _period_kind(report_date):
    text = _date(report_date)
    if not text:
        return None
    month_day = text[5:]
    return {
        "03-31": "Q1",
        "06-30": "H1",
        "09-30": "Q3",
        "12-31": "FY",
    }.get(month_day)


def _period_key(report_date):
    date = _date(report_date)
    kind = _period_kind(date)
    return f"{date[:4]}{kind}" if date and kind else date


def _rows_by_period(rows):
    out = {}
    for row in rows or []:
        report_date = _date(_get(row, "REPORT_DATE", "REPORTDATE"))
        if not report_date:
            continue
        # The API can expose multiple statement variants for one period. Keep
        # the first item after provider's descending sort; prefer consolidated
        # records when the marker is explicit.
        current = out.get(report_date)
        if current is None:
            out[report_date] = row
            continue
        new_type = str(_get(row, "REPORT_TYPE", "REPORTTYPE", "DATATYPE") or "").upper()
        old_type = str(_get(current, "REPORT_TYPE", "REPORTTYPE", "DATATYPE") or "").upper()
        if "合并" in new_type and "合并" not in old_type:
            out[report_date] = row
    return out


def _reported_metric(main, income, balance, cash):
    revenue = _as_float(_get(main, "TOTAL_OPERATE_INCOME"))
    if revenue is None:
        revenue = _as_float(_get(income, "TOTAL_OPERATE_INCOME", "OPERATE_INCOME", "OPERATING_REVENUE"))
    parent_profit = _as_float(_get(main, "PARENT_NETPROFIT"))
    if parent_profit is None:
        parent_profit = _as_float(_get(income, "PARENT_NETPROFIT", "PARENT_NET_PROFIT", "NETPROFIT_PARENT"))
    adjusted = _as_float(_get(main, "DEDUCT_PARENT_NETPROFIT", "DEDUCT_NETPROFIT"))
    if adjusted is None:
        adjusted = _as_float(_get(income, "DEDUCT_PARENT_NETPROFIT", "DEDUCT_NETPROFIT"))
    operating_profit = _as_float(_get(income, "OPERATE_PROFIT", "OPERATING_PROFIT"))
    gross_profit = _as_float(_get(income, "GROSS_PROFIT", "OPERATE_GROSS_PROFIT"))
    gross_margin = _as_float(_get(main, "XSMLL", "GROSS_MARGIN"))
    roe = _as_float(_get(main, "WEIGHTAVG_ROE", "ROE_WEIGHTED"))

    cfo = _as_float(_get(cash, "NETCASH_OPERATE", "NET_CASH_OPERATE", "NET_CASHFLOW_OPERATE"))
    cfi = _as_float(_get(cash, "NETCASH_INVEST", "NET_CASH_INVEST", "NET_CASHFLOW_INVEST"))
    cff = _as_float(_get(cash, "NETCASH_FINANCE", "NET_CASH_FINANCE", "NET_CASHFLOW_FINANCE"))

    assets = _as_float(_get(balance, "TOTAL_ASSETS", "ASSET_TOTAL"))
    liabilities = _as_float(_get(balance, "TOTAL_LIABILITIES", "TOTAL_LIABILITY", "LIABILITY_TOTAL"))
    equity = _as_float(_get(balance, "TOTAL_EQUITY", "TOTAL_PARENT_EQUITY", "EQUITY_TOTAL", "PARENT_EQUITY"))
    cash_balance = _as_float(_get(balance, "MONETARYFUNDS", "MONETARY_FUNDS", "CASH_AND_CASH_EQUIVALENTS"))
    receivables = _as_float(_get(balance, "ACCOUNTS_RECE", "ACCOUNTS_RECEIVABLE", "NOTE_ACCOUNTS_RECE"))
    inventory = _as_float(_get(balance, "INVENTORY"))
    goodwill = _as_float(_get(balance, "GOODWILL"))
    short_debt = _as_float(_get(balance, "SHORT_LOAN", "SHORT_TERM_LOAN"))
    long_debt = _as_float(_get(balance, "LONG_LOAN", "LONG_TERM_LOAN"))
    bonds = _as_float(_get(balance, "BOND_PAYABLE", "BONDS_PAYABLE"))
    interest_debt_parts = [x for x in (short_debt, long_debt, bonds) if x is not None]
    interest_bearing_debt = sum(interest_debt_parts) if interest_debt_parts else None

    published = _iso(_get(main, "NOTICE_DATE", "UPDATE_DATE", "EITIME"))
    if not published:
        published = _iso(_get(income, "NOTICE_DATE", "UPDATE_DATE"))
    return {
        "income": {
            "revenue": revenue,
            "parent_net_profit": parent_profit,
            "adjusted_net_profit": adjusted,
            "operating_profit": operating_profit,
            "gross_profit": gross_profit,
            "revenue_yoy_percent_reported": _as_float(_get(main, "YSTZ")),
            "revenue_qoq_percent_reported": _as_float(_get(main, "YSHZ")),
            "parent_net_profit_yoy_percent_reported": _as_float(_get(main, "SJLTZ")),
            "parent_net_profit_qoq_percent_reported": _as_float(_get(main, "SJLHZ")),
        },
        "profitability": {
            "gross_margin_percent_reported": gross_margin,
            "weighted_roe_percent_reported": roe,
            "operating_margin_percent_derived": _round(_ratio(operating_profit, revenue) * 100.0, 4) if _ratio(operating_profit, revenue) is not None else None,
            "net_margin_percent_derived": _round(_ratio(parent_profit, revenue) * 100.0, 4) if _ratio(parent_profit, revenue) is not None else None,
        },
        "cashflow": {
            "operating_cash_flow": cfo,
            "investing_cash_flow": cfi,
            "financing_cash_flow": cff,
            "operating_cash_flow_to_parent_profit": _round(_ratio(cfo, parent_profit), 4),
        },
        "balance_sheet": {
            "total_assets": assets,
            "total_liabilities": liabilities,
            "equity": equity,
            "cash": cash_balance,
            "receivables": receivables,
            "inventory": inventory,
            "goodwill": goodwill,
            "interest_bearing_debt": interest_bearing_debt,
            "debt_to_assets_percent": _round(_ratio(liabilities, assets) * 100.0, 4) if _ratio(liabilities, assets) is not None else None,
            "cash_to_interest_bearing_debt": _round(_ratio(cash_balance, interest_bearing_debt), 4),
        },
        "published_at": published,
    }


def _normalize_reports(raw, first_seen_by_version, now_iso):
    maps = {key: _rows_by_period(rows) for key, rows in (raw or {}).items()}
    dates = sorted(set().union(*(set(value) for value in maps.values())), reverse=True)
    periods = []
    for report_date in dates:
        kind = _period_kind(report_date)
        if not kind:
            continue
        metrics = _reported_metric(
            maps.get("main", {}).get(report_date),
            maps.get("income", {}).get(report_date),
            maps.get("balance", {}).get(report_date),
            maps.get("cashflow", {}).get(report_date),
        )
        published = metrics.pop("published_at", None)
        version_key = f"{report_date}|{published or 'UNKNOWN'}"
        first_seen = first_seen_by_version.get(version_key)
        if not first_seen:
            first_seen = now_iso
            first_seen_by_version[version_key] = first_seen
        periods.append({
            "report_period_end": report_date,
            "period_key": _period_key(report_date),
            "period_kind": kind,
            "reported_scope": "REPORTED_CUMULATIVE",
            "published_at": published,
            "first_seen_at": first_seen,
            "income": metrics["income"],
            "profitability": metrics["profitability"],
            "cashflow": metrics["cashflow"],
            "balance_sheet": metrics["balance_sheet"],
        })
    return periods


def _diff_group(current, previous, fields):
    out = {}
    for field in fields:
        out[field] = _sub((current or {}).get(field), (previous or {}).get(field))
    return out


def _normalize_single_quarters(periods):
    by_year_kind = {}
    for item in periods:
        date = item.get("report_period_end")
        kind = item.get("period_kind")
        if date and kind:
            by_year_kind[(date[:4], kind)] = item
    out = []
    flow_groups = {
        "income": ["revenue", "parent_net_profit", "adjusted_net_profit", "operating_profit", "gross_profit"],
        "cashflow": ["operating_cash_flow", "investing_cash_flow", "financing_cash_flow"],
    }
    for item in sorted(periods, key=lambda x: x.get("report_period_end") or ""):
        year = item["report_period_end"][:4]
        kind = item["period_kind"]
        previous_kind = {"Q1": None, "H1": "Q1", "Q3": "H1", "FY": "Q3"}[kind]
        predecessor = by_year_kind.get((year, previous_kind)) if previous_kind else None
        if previous_kind and predecessor is None:
            continue
        value = {
            "report_period_end": item["report_period_end"],
            "period_key": item["period_key"],
            "period_kind": kind,
            "reported_scope": "NORMALIZED_SINGLE_QUARTER",
            "normalization": {
                "verified": True,
                "method": "REPORTED_CUMULATIVE" if kind == "Q1" else f"{kind}_CUMULATIVE_MINUS_{previous_kind}_CUMULATIVE",
                "source_periods": [item["report_period_end"]] if predecessor is None else [item["report_period_end"], predecessor["report_period_end"]],
            },
            "income": {},
            "cashflow": {},
        }
        for group, fields in flow_groups.items():
            current = item.get(group) or {}
            previous = (predecessor or {}).get(group) or {}
            for field in fields:
                current_value = _as_float(current.get(field))
                if predecessor is None:
                    normalized = current_value
                else:
                    normalized = _sub(current_value, previous.get(field))
                value[group][field] = normalized
        revenue = value["income"].get("revenue")
        profit = value["income"].get("parent_net_profit")
        cfo = value["cashflow"].get("operating_cash_flow")
        value["profitability"] = {
            "net_margin_percent_derived": _round(_ratio(profit, revenue) * 100.0, 4) if _ratio(profit, revenue) is not None else None,
            "operating_cash_flow_to_parent_profit": _round(_ratio(cfo, profit), 4),
        }
        out.append(value)

    by_kind = {item["period_key"]: item for item in out}
    for item in out:
        year = int(item["report_period_end"][:4])
        prior_key = f"{year - 1}{item['period_kind']}"
        prior = by_kind.get(prior_key)
        item["yoy"] = {
            "revenue_percent": _round(_pct((item.get("income") or {}).get("revenue"), ((prior or {}).get("income") or {}).get("revenue")), 4),
            "parent_net_profit_percent": _round(_pct((item.get("income") or {}).get("parent_net_profit"), ((prior or {}).get("income") or {}).get("parent_net_profit")), 4),
            "adjusted_net_profit_percent": _round(_pct((item.get("income") or {}).get("adjusted_net_profit"), ((prior or {}).get("income") or {}).get("adjusted_net_profit")), 4),
            "operating_cash_flow_percent": _round(_pct((item.get("cashflow") or {}).get("operating_cash_flow"), ((prior or {}).get("cashflow") or {}).get("operating_cash_flow")), 4),
        }
    return sorted(out, key=lambda x: x["report_period_end"], reverse=True)


def _quarter_ordinal(item):
    date = item.get("report_period_end")
    kind = item.get("period_kind")
    if not date or kind not in PERIOD_ORDER:
        return None
    return int(date[:4]) * 4 + PERIOD_ORDER[kind] - 1


def _ttm(single_quarters):
    ordered = sorted(single_quarters, key=lambda x: x.get("report_period_end") or "")
    if len(ordered) < 4:
        return {"status": "UNAVAILABLE", "reason": "FEWER_THAN_4_VERIFIED_SINGLE_QUARTERS"}
    selected = ordered[-4:]
    ordinals = [_quarter_ordinal(x) for x in selected]
    if any(x is None for x in ordinals) or any(ordinals[i] + 1 != ordinals[i + 1] for i in range(3)):
        return {"status": "UNAVAILABLE", "reason": "NON_CONSECUTIVE_SINGLE_QUARTERS"}
    def total(group, field):
        values = [_as_float((x.get(group) or {}).get(field)) for x in selected]
        return sum(values) if all(x is not None for x in values) else None
    revenue = total("income", "revenue")
    profit = total("income", "parent_net_profit")
    adjusted = total("income", "adjusted_net_profit")
    cfo = total("cashflow", "operating_cash_flow")
    return {
        "status": "OK",
        "reported_scope": "TTM",
        "through_period_end": selected[-1]["report_period_end"],
        "source_periods": [x["report_period_end"] for x in selected],
        "income": {
            "revenue": revenue,
            "parent_net_profit": profit,
            "adjusted_net_profit": adjusted,
        },
        "cashflow": {"operating_cash_flow": cfo},
        "profitability": {
            "net_margin_percent_derived": _round(_ratio(profit, revenue) * 100.0, 4) if _ratio(profit, revenue) is not None else None,
            "operating_cash_flow_to_parent_profit": _round(_ratio(cfo, profit), 4),
        },
    }


def _trend(values):
    vals = [float(x) for x in values if x is not None]
    if len(vals) < 3:
        return "UNKNOWN"
    recent = vals[-3:]
    changes = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
    scale = max(statistics.fmean(abs(x) for x in recent), 1e-9)
    dispersion = (max(recent) - min(recent)) / scale
    if dispersion > 1.5 and changes[0] * changes[1] < 0:
        return "VOLATILE"
    if changes[-1] > 0 and changes[-1] >= changes[0]:
        return "ACCELERATING"
    if all(x > 0 for x in changes):
        return "IMPROVING"
    if changes[-1] < 0 and changes[-1] <= changes[0]:
        return "DETERIORATING"
    if all(x < 0 for x in changes):
        return "SLOWING"
    if max(abs(x) for x in changes) / scale < 0.08:
        return "STABLE"
    return "VOLATILE"


def _trend_summary(single_quarters, reported_periods):
    chronological = sorted(single_quarters, key=lambda x: x.get("report_period_end") or "")[-8:]
    reported_chrono = sorted(reported_periods, key=lambda x: x.get("report_period_end") or "")[-8:]
    return {
        "revenue": _trend([(x.get("income") or {}).get("revenue") for x in chronological]),
        "parent_net_profit": _trend([(x.get("income") or {}).get("parent_net_profit") for x in chronological]),
        "adjusted_net_profit": _trend([(x.get("income") or {}).get("adjusted_net_profit") for x in chronological]),
        "net_margin": _trend([(x.get("profitability") or {}).get("net_margin_percent_derived") for x in chronological]),
        "operating_cash_flow": _trend([(x.get("cashflow") or {}).get("operating_cash_flow") for x in chronological]),
        "weighted_roe": _trend([(x.get("profitability") or {}).get("weighted_roe_percent_reported") for x in reported_chrono]),
        "window_single_quarters": len(chronological),
        "method": "deterministic recent-three-point direction/acceleration with volatility guard",
    }


def _balance_yoy(latest, periods, field):
    if not latest:
        return None
    year = int(latest["report_period_end"][:4])
    key = f"{year - 1}{latest['period_kind']}"
    prior = next((x for x in periods if x.get("period_key") == key), None)
    return _pct((latest.get("balance_sheet") or {}).get(field), ((prior or {}).get("balance_sheet") or {}).get(field))


def _divergences(reported_periods, single_quarters):
    signals = []
    latest_reported = reported_periods[0] if reported_periods else None
    latest_single = single_quarters[0] if single_quarters else None
    if latest_single:
        yoy = latest_single.get("yoy") or {}
        profit_yoy = _as_float(yoy.get("parent_net_profit_percent"))
        cfo_yoy = _as_float(yoy.get("operating_cash_flow_percent"))
        revenue_yoy = _as_float(yoy.get("revenue_percent"))
        margin = _as_float((latest_single.get("profitability") or {}).get("net_margin_percent_derived"))
        prior_same = None
        year = int(latest_single["report_period_end"][:4])
        prior_key = f"{year - 1}{latest_single['period_kind']}"
        prior_same = next((x for x in single_quarters if x.get("period_key") == prior_key), None)
        prior_margin = _as_float(((prior_same or {}).get("profitability") or {}).get("net_margin_percent_derived"))
        if profit_yoy is not None and cfo_yoy is not None and profit_yoy > 10 and cfo_yoy < 0:
            signals.append({"code": "PROFIT_UP_CASHFLOW_DOWN", "evidence": {"profit_yoy_percent": profit_yoy, "operating_cash_flow_yoy_percent": cfo_yoy}})
        if revenue_yoy is not None and revenue_yoy > 5 and margin is not None and prior_margin is not None and margin < prior_margin - 1.0:
            signals.append({"code": "MARGIN_DOWN_REVENUE_UP", "evidence": {"revenue_yoy_percent": revenue_yoy, "net_margin_percent": margin, "prior_net_margin_percent": prior_margin}})
    if latest_reported:
        revenue_yoy = _as_float((latest_reported.get("income") or {}).get("revenue_yoy_percent_reported"))
        receivables_yoy = _balance_yoy(latest_reported, reported_periods, "receivables")
        if revenue_yoy is not None and receivables_yoy is not None and revenue_yoy > 0 and receivables_yoy > revenue_yoy + 15:
            signals.append({"code": "REVENUE_UP_RECEIVABLES_FASTER", "evidence": {"revenue_yoy_percent": revenue_yoy, "receivables_yoy_percent": _round(receivables_yoy, 4)}})
        liabilities_yoy = _balance_yoy(latest_reported, reported_periods, "total_liabilities")
        cash_yoy = _balance_yoy(latest_reported, reported_periods, "cash")
        if liabilities_yoy is not None and cash_yoy is not None and liabilities_yoy > 10 and cash_yoy < 0:
            signals.append({"code": "LEVERAGE_RISING_CASH_FALLING", "evidence": {"liabilities_yoy_percent": _round(liabilities_yoy, 4), "cash_yoy_percent": _round(cash_yoy, 4)}})
    return signals


def _event_refresh_trigger(item, cache_fetched_at):
    events = ((item or {}).get("events") or {}).get("recent") or []
    cache_time = None
    try:
        cache_time = datetime.fromisoformat(str(cache_fetched_at).replace("Z", "+00:00")) if cache_fetched_at else None
    except ValueError:
        pass
    candidates = []
    for event in events:
        if str((event or {}).get("event_type")) != "PERIODIC_REPORT":
            continue
        published = (event or {}).get("published_at")
        try:
            dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            dt = None
        if dt and (cache_time is None or dt > cache_time):
            candidates.append((event or {}).get("event_id"))
    return {
        "recommended": bool(candidates),
        "reason": "PERIODIC_REPORT_EVENT_AFTER_CACHE" if candidates else "NO_NEW_PERIODIC_REPORT_SIGNAL",
        "event_ids": candidates,
    }


def _metadata(latest, fetched_at, status, provider_error=None):
    quality = "PASS" if status == "OK" else "DEGRADED" if status == "CACHED" else "PARTIAL"
    flags = []
    if status == "CACHED":
        flags.append("FAST_CACHE_ONLY")
    if provider_error:
        flags.append("PROVIDER_ERROR")
    return data_metadata._metadata(
        "Eastmoney",
        fetched_at,
        data_time=(latest or {}).get("published_at"),
        freshness="LATEST_REPORTED_PERIOD" if latest else "UNAVAILABLE",
        freshness_policy="FUNDAMENTALS",
        quality=quality,
        quality_flags=flags,
        source_tier="PRIMARY_PROVIDER",
        data_class="FUNDAMENTALS",
        first_seen_at=(latest or {}).get("first_seen_at"),
    )


def _build_context(code, item, raw, cache, urls, errors, now_iso, execution_mode):
    first_seen = dict((cache or {}).get("first_seen_by_version") or {})
    periods = _normalize_reports(raw, first_seen, now_iso)
    single = _normalize_single_quarters(periods)
    ttm = _ttm(single)
    latest = periods[0] if periods else None
    status = "OK" if periods and not errors and execution_mode == "FULL" else "CACHED" if periods and execution_mode != "FULL" else "DEGRADED" if periods else "UNAVAILABLE"
    trends = _trend_summary(single, periods)
    divergences = _divergences(periods, single)
    refresh_trigger = _event_refresh_trigger(item, (cache or {}).get("fetched_at"))
    metadata = _metadata(latest, (cache or {}).get("fetched_at") or now_iso if execution_mode != "FULL" else now_iso, status, "; ".join(errors) if errors else None)
    return {
        "status": status,
        "version": FUNDAMENTALS_VERSION,
        "source": "Eastmoney",
        "source_tier": "PRIMARY_PROVIDER",
        "source_semantics": "company-reported financial statements redistributed by a market-data provider",
        "latest_report_period_end": (latest or {}).get("report_period_end"),
        "latest_published_at": (latest or {}).get("published_at"),
        "fetched_at": now_iso if execution_mode == "FULL" else (cache or {}).get("fetched_at"),
        "reported_periods": periods[:12],
        "single_quarters": single[:8],
        "ttm": ttm,
        "trends": trends,
        "divergence_signals": divergences,
        "refresh_trigger": refresh_trigger,
        "peer_comparison": {
            "status": "DEFERRED_V1",
            "reason": "Avoid additional low-value peer financial network requests on the realtime pipeline; current group comparison remains market/flow based.",
        },
        "coverage": {
            "reported_period_count": len(periods),
            "verified_single_quarter_count": len(single),
            "ttm_available": ttm.get("status") == "OK",
            "provider_report_rows": {key: len(value or []) for key, value in (raw or {}).items()},
        },
        "provider_health": {
            "status": "OK" if not errors else "PARTIAL" if periods else "ERROR",
            "errors": errors,
            "source_urls": urls,
        },
        "metadata": metadata,
        "provenance": {
            "type": "COMPOSITE",
            "provider": "Eastmoney",
            "reported_scope": "REPORTED_CUMULATIVE",
            "normalization_rules": "Q1 direct; H1-Q1; Q3-H1; FY-Q3; TTM=sum of four consecutive verified single quarters",
        },
        "first_seen_by_version": first_seen,
    }


def finalize_snapshot(snapshot_path, base, execution_mode):
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    now_text = snapshot.get("runner_time_cst") or snapshot.get("runner_time_utc")
    try:
        now = datetime.fromisoformat(str(now_text).replace("Z", "+00:00"))
    except Exception:
        now = datetime.now(base.CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=base.CST)
    now_iso = now.isoformat(timespec="seconds")

    def build_one(code, item):
        cache_path = _cache_path(code)
        cache = _load_json(cache_path) or {}
        raw = cache.get("raw") if isinstance(cache.get("raw"), dict) else {}
        urls = cache.get("source_urls") if isinstance(cache.get("source_urls"), dict) else {}
        errors = []
        if execution_mode == "FULL":
            fresh_raw, fresh_urls, fresh_errors = fetch_all_reports(base, code)
            if any(fresh_raw.get(key) for key in REPORTS):
                raw = fresh_raw
                urls = fresh_urls
            errors.extend(fresh_errors)
        context = _build_context(code, item, raw, cache, urls, errors, now_iso, execution_mode)
        if execution_mode == "FULL" and raw:
            payload = {
                "schema_version": CACHE_SCHEMA,
                "code": code,
                "source": "Eastmoney",
                "source_tier": "PRIMARY_PROVIDER",
                "fetched_at": now_iso,
                "first_seen_by_version": context.pop("first_seen_by_version"),
                "source_urls": urls,
                "raw": raw,
            }
            _write_json(cache_path, payload)
            context["cache"] = {"state": "REFRESHED", "path": str(cache_path)}
        else:
            context.pop("first_seen_by_version", None)
            context["cache"] = {
                "state": "HIT" if raw else "MISS",
                "path": str(cache_path),
                "cache_fetched_at": cache.get("fetched_at"),
            }
        return code, context

    detail = snapshot.get("detail_stocks") or {}
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, min(2, len(detail)))) as pool:
        futures = [pool.submit(build_one, code, item) for code, item in detail.items()]
        for future in as_completed(futures):
            code, context = future.result()
            results[code] = context

    for code, context in results.items():
        detail[code]["fundamentals"] = context
        print(
            "FUNDAMENTALS "
            f"{code} status={context.get('status')} latest={context.get('latest_report_period_end')} "
            f"single_q={((context.get('coverage') or {}).get('verified_single_quarter_count'))} "
            f"ttm={((context.get('ttm') or {}).get('status'))} trends={context.get('trends')} "
            f"divergences={len(context.get('divergence_signals') or [])}",
            flush=True,
        )

    snapshot["schema_version"] = max(int(snapshot.get("schema_version") or 0), 15)
    snapshot.setdefault("features", {})["fundamentals"] = FUNDAMENTALS_VERSION
    snapshot["fundamentals_summary"] = {
        "status": "OK" if results and all(x.get("status") == "OK" for x in results.values()) else "PARTIAL" if results else "UNAVAILABLE",
        "detail_stock_count": len(results),
        "status_by_code": {code: value.get("status") for code, value in sorted(results.items())},
        "fast_path_policy": "CACHE_ONLY_NO_FINANCIAL_NETWORK",
        "single_quarter_contract": "ONLY_VERIFIED_CUMULATIVE_DIFFERENCING",
        "ttm_contract": "FOUR_CONSECUTIVE_VERIFIED_SINGLE_QUARTERS",
    }
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print("SNAPSHOT_SCHEMA_UPGRADED schema_version=15 feature=fundamentals:v1", flush=True)
