import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path


OWNERSHIP_CAPITAL_VERSION = "v1"
SNAPSHOT_SCHEMA_VERSION = 17
CAPITAL_STRUCTURE_ENDPOINT = (
    "https://emweb.securities.eastmoney.com/PC_HSF10/CapitalStockStructure/PageAjax"
)
CST = timezone(timedelta(hours=8))


def _as_float(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value):
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else None


def _round(value, digits=4):
    return None if value is None else round(float(value), digits)


def _ratio_percent(numerator, denominator):
    numerator = _as_float(numerator)
    denominator = _as_float(denominator)
    if numerator is None or denominator in (None, 0):
        return None
    return _round(numerator / denominator * 100.0, 4)


def _runner_time_iso(snapshot):
    utc_value = snapshot.get("runner_time_utc")
    if utc_value:
        try:
            dt = datetime.fromisoformat(str(utc_value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(CST).isoformat(timespec="seconds")
        except ValueError:
            pass
    cst_value = snapshot.get("runner_time_cst")
    if cst_value:
        text = str(cst_value).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST)
            return dt.astimezone(CST).isoformat(timespec="seconds")
        except ValueError:
            try:
                dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
                return dt.isoformat(timespec="seconds")
            except ValueError:
                pass
    return datetime.now(CST).isoformat(timespec="seconds")


def _provider_code(base, code):
    market, _, _ = base.infer_identifiers(code)
    return f"{market}{code}"


def fetch_share_structure(base, code):
    provider_code = _provider_code(base, code)
    url = CAPITAL_STRUCTURE_ENDPOINT + "?" + urllib.parse.urlencode({"code": provider_code})
    payload = json.loads(base.http_get(url))
    if not isinstance(payload, dict):
        raise RuntimeError("Eastmoney capital-structure response is not an object")
    return payload, url, provider_code


def _dated_candidates(payload):
    candidates = []
    for section in ("lngbbd", "gbjg"):
        rows = (payload or {}).get(section) or []
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            as_of = _date(row.get("END_DATE") or row.get("CHANGE_DATE") or row.get("REPORT_DATE"))
            if as_of:
                candidates.append((as_of, section, index, row))
    return sorted(candidates, key=lambda item: (item[0], item[1] == "lngbbd"), reverse=True)


def normalize_share_structure(payload, source_url, provider_code, fetched_at):
    candidates = _dated_candidates(payload)
    if not candidates:
        return {
            "status": "UNAVAILABLE",
            "as_of_date": None,
            "values": None,
            "metadata": {
                "freshness": "UNAVAILABLE",
                "realtime": False,
                "quality": "FAILED",
                "quality_flags": ["NO_DATED_SHARE_STRUCTURE_ROW"],
            },
            "provenance": {
                "provider": "Eastmoney",
                "source_tier": "PRIMARY_PROVIDER",
                "endpoint": CAPITAL_STRUCTURE_ENDPOINT,
                "source_url": source_url,
                "provider_code": provider_code,
                "fetched_at": fetched_at,
                "raw_section": None,
                "raw_row_index": None,
            },
        }

    as_of_date, section, row_index, row = candidates[0]
    total_shares = _as_float(row.get("TOTAL_SHARES"))
    listed_a_shares = _as_float(row.get("LISTED_A_SHARES"))
    unrestricted_shares = _as_float(row.get("UNLIMITED_SHARES"))
    restricted_shares = _as_float(row.get("LIMITED_SHARES"))
    free_float_shares = _as_float(row.get("FREE_SHARES"))

    values = {
        "total_shares": total_shares,
        "float_shares": listed_a_shares,
        "float_scope": "LISTED_A_SHARES",
        "unrestricted_shares": unrestricted_shares,
        "restricted_shares": restricted_shares,
        "free_float_shares": free_float_shares,
        "float_ratio_percent": _ratio_percent(listed_a_shares, total_shares),
        "unrestricted_ratio_percent": _ratio_percent(unrestricted_shares, total_shares),
        "restricted_ratio_percent": _ratio_percent(restricted_shares, total_shares),
        "free_float_ratio_percent": _ratio_percent(free_float_shares, total_shares),
        "change_reason": row.get("CHANGE_REASON") or None,
    }
    required = ("total_shares", "float_shares", "restricted_shares")
    missing = [field for field in required if values.get(field) is None]
    quality = "PASS" if not missing else "PARTIAL"
    return {
        "status": "OK" if not missing else "PARTIAL",
        "as_of_date": as_of_date,
        "values": values,
        "metadata": {
            "freshness": "LATEST_DISCLOSED_SHARE_STRUCTURE",
            "realtime": False,
            "quality": quality,
            "quality_flags": [f"MISSING_{name.upper()}" for name in missing],
            "date_semantics": "SHARE_STRUCTURE_EFFECTIVE_OR_DISCLOSED_DATE",
        },
        "provenance": {
            "provider": "Eastmoney",
            "source_tier": "PRIMARY_PROVIDER",
            "endpoint": CAPITAL_STRUCTURE_ENDPOINT,
            "source_url": source_url,
            "provider_code": provider_code,
            "fetched_at": fetched_at,
            "raw_section": section,
            "raw_row_index": row_index,
            "raw_date_field": "END_DATE",
            "field_mapping": {
                "total_shares": "TOTAL_SHARES",
                "float_shares": "LISTED_A_SHARES",
                "unrestricted_shares": "UNLIMITED_SHARES",
                "restricted_shares": "LIMITED_SHARES",
                "free_float_shares": "FREE_SHARES",
            },
            "derived_fields": {
                "float_ratio_percent": "LISTED_A_SHARES / TOTAL_SHARES * 100",
                "unrestricted_ratio_percent": "UNLIMITED_SHARES / TOTAL_SHARES * 100",
                "restricted_ratio_percent": "LIMITED_SHARES / TOTAL_SHARES * 100",
                "free_float_ratio_percent": "FREE_SHARES / TOTAL_SHARES * 100",
            },
        },
    }


def _deferred_context(fetched_at):
    return {
        "version": OWNERSHIP_CAPITAL_VERSION,
        "status": "DEFERRED",
        "share_structure": {
            "status": "DEFERRED",
            "as_of_date": None,
            "values": None,
            "metadata": {
                "freshness": "NOT_FETCHED_IN_INTRADAY_FAST",
                "realtime": False,
                "quality": "PARTIAL",
                "quality_flags": ["FULL_ONLY_INITIAL_SHARE_STRUCTURE_SLICE"],
            },
            "provenance": {
                "provider": "Eastmoney",
                "source_tier": "PRIMARY_PROVIDER",
                "endpoint": CAPITAL_STRUCTURE_ENDPOINT,
                "source_url": None,
                "provider_code": None,
                "fetched_at": fetched_at,
                "raw_section": None,
                "raw_row_index": None,
            },
        },
    }


def _fetch_one(base, code, fetched_at):
    try:
        payload, url, provider_code = fetch_share_structure(base, code)
        share_structure = normalize_share_structure(payload, url, provider_code, fetched_at)
        return code, {
            "version": OWNERSHIP_CAPITAL_VERSION,
            "status": share_structure["status"],
            "share_structure": share_structure,
        }
    except Exception as exc:
        return code, {
            "version": OWNERSHIP_CAPITAL_VERSION,
            "status": "UNAVAILABLE",
            "share_structure": {
                "status": "UNAVAILABLE",
                "as_of_date": None,
                "values": None,
                "metadata": {
                    "freshness": "UNAVAILABLE",
                    "realtime": False,
                    "quality": "FAILED",
                    "quality_flags": ["PROVIDER_ERROR"],
                    "error": f"{type(exc).__name__}: {exc}",
                },
                "provenance": {
                    "provider": "Eastmoney",
                    "source_tier": "PRIMARY_PROVIDER",
                    "endpoint": CAPITAL_STRUCTURE_ENDPOINT,
                    "source_url": None,
                    "provider_code": None,
                    "fetched_at": fetched_at,
                    "raw_section": None,
                    "raw_row_index": None,
                },
            },
        }


def finalize_snapshot(snapshot_path, base, execution_mode):
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    fetched_at = _runner_time_iso(snapshot)
    detail = snapshot.get("detail_stocks") or {}
    results = {}

    if str(execution_mode or "").upper() != "FULL":
        for code in detail:
            results[code] = _deferred_context(fetched_at)
    elif detail:
        with ThreadPoolExecutor(max_workers=max(1, min(2, len(detail)))) as pool:
            futures = [pool.submit(_fetch_one, base, code, fetched_at) for code in detail]
            for future in as_completed(futures):
                code, context = future.result()
                results[code] = context

    for code, context in results.items():
        detail[code]["ownership_and_capital"] = context
        share = context.get("share_structure") or {}
        values = share.get("values") or {}
        print(
            "OWNERSHIP_CAPITAL "
            f"{code} status={share.get('status')} as_of={share.get('as_of_date')} "
            f"total={values.get('total_shares')} float_a={values.get('float_shares')} "
            f"restricted={values.get('restricted_shares')}",
            flush=True,
        )

    snapshot["schema_version"] = max(int(snapshot.get("schema_version") or 0), SNAPSHOT_SCHEMA_VERSION)
    snapshot.setdefault("features", {})["ownership_and_capital"] = OWNERSHIP_CAPITAL_VERSION
    snapshot["ownership_and_capital_summary"] = {
        "status": (
            "OK"
            if results and all(value.get("status") == "OK" for value in results.values())
            else "DEFERRED"
            if results and all(value.get("status") == "DEFERRED" for value in results.values())
            else "PARTIAL"
            if results
            else "UNAVAILABLE"
        ),
        "detail_stock_count": len(results),
        "status_by_code": {code: value.get("status") for code, value in sorted(results.items())},
        "implemented_sections": ["share_structure"],
        "share_structure_contract": "DATED_PROVIDER_ROW; FLOAT_SHARES=LISTED_A_SHARES; RATIOS_DERIVED",
        "intraday_fast_policy": "DEFER_NETWORK_UNTIL_CACHE_CONTINUITY_SLICE",
    }
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"SNAPSHOT_SCHEMA_UPGRADED schema_version={SNAPSHOT_SCHEMA_VERSION} "
        f"feature=ownership_and_capital:{OWNERSHIP_CAPITAL_VERSION}",
        flush=True,
    )
