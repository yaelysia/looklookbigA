import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path


OWNERSHIP_CAPITAL_VERSION = "v1"
SNAPSHOT_SCHEMA_VERSION = 17
SHARE_STRUCTURE_HISTORY_LIMIT = 12
CAPITAL_STRUCTURE_ENDPOINT = (
    "https://emweb.securities.eastmoney.com/PC_HSF10/CapitalStockStructure/PageAjax"
)
SHAREHOLDER_RESEARCH_ENDPOINT = (
    "https://emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/PageAjax"
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


def _fetch_json_object(base, endpoint, provider_code):
    url = endpoint + "?" + urllib.parse.urlencode({"code": provider_code})
    payload = json.loads(base.http_get(url))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Eastmoney response is not an object: {endpoint}")
    return payload, url


def fetch_share_structure(base, code):
    provider_code = _provider_code(base, code)
    payload, url = _fetch_json_object(base, CAPITAL_STRUCTURE_ENDPOINT, provider_code)
    return payload, url, provider_code


def fetch_controllers(base, code):
    provider_code = _provider_code(base, code)
    payload, url = _fetch_json_object(base, SHAREHOLDER_RESEARCH_ENDPOINT, provider_code)
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


def _share_values(row):
    total_shares = _as_float(row.get("TOTAL_SHARES"))
    listed_a_shares = _as_float(row.get("LISTED_A_SHARES"))
    unrestricted_shares = _as_float(row.get("UNLIMITED_SHARES"))
    restricted_shares = _as_float(row.get("LIMITED_SHARES"))
    free_float_shares = _as_float(row.get("FREE_SHARES"))
    return {
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


def _numeric_delta(current, previous):
    current = _as_float(current)
    previous = _as_float(previous)
    if current is None or previous is None:
        return {"delta": None, "change_percent": None, "comparable": False}
    delta = current - previous
    return {
        "delta": _round(delta, 4),
        "change_percent": _round(delta / abs(previous) * 100.0, 4) if previous else None,
        "comparable": True,
    }


def _share_structure_history(candidates):
    history = []
    seen_dates = set()
    for as_of_date, section, row_index, row in candidates:
        if as_of_date in seen_dates:
            continue
        seen_dates.add(as_of_date)
        values = _share_values(row)
        missing = [
            field
            for field in ("total_shares", "float_shares", "restricted_shares")
            if values.get(field) is None
        ]
        history.append(
            {
                "as_of_date": as_of_date,
                "values": values,
                "status": "OK" if not missing else "PARTIAL",
                "quality_flags": [f"MISSING_{name.upper()}" for name in missing],
                "source": {"raw_section": section, "raw_row_index": row_index},
                "change_from_previous": None,
            }
        )
        if len(history) >= SHARE_STRUCTURE_HISTORY_LIMIT:
            break

    fields = (
        "total_shares",
        "float_shares",
        "unrestricted_shares",
        "restricted_shares",
        "free_float_shares",
    )
    for index, period in enumerate(history[:-1]):
        previous = history[index + 1]
        period["change_from_previous"] = {
            "previous_as_of_date": previous["as_of_date"],
            **{
                field: _numeric_delta(period["values"].get(field), previous["values"].get(field))
                for field in fields
            },
        }
    return history


def _share_structure_trend(history):
    if len(history) < 2:
        return {
            "state": "UNKNOWN",
            "latest_as_of_date": history[0]["as_of_date"] if history else None,
            "baseline_as_of_date": None,
            "total_shares": {"delta": None, "change_percent": None, "comparable": False},
            "float_shares": {"delta": None, "change_percent": None, "comparable": False},
        }
    latest, previous = history[:2]
    total = _numeric_delta(
        latest["values"].get("total_shares"), previous["values"].get("total_shares")
    )
    floating = _numeric_delta(
        latest["values"].get("float_shares"), previous["values"].get("float_shares")
    )
    delta = total.get("delta")
    state = "UNKNOWN" if delta is None else "RISING" if delta > 0 else "FALLING" if delta < 0 else "STABLE"
    return {
        "state": state,
        "latest_as_of_date": latest["as_of_date"],
        "baseline_as_of_date": previous["as_of_date"],
        "total_shares": total,
        "float_shares": floating,
    }


def normalize_share_structure(payload, source_url, provider_code, fetched_at):
    candidates = _dated_candidates(payload)
    if not candidates:
        return {
            "status": "UNAVAILABLE",
            "as_of_date": None,
            "values": None,
            "history": [],
            "trend": _share_structure_trend([]),
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
    history = _share_structure_history(candidates)
    values = history[0]["values"]
    required = ("total_shares", "float_shares", "restricted_shares")
    missing = [field for field in required if values.get(field) is None]
    quality = "PASS" if not missing else "PARTIAL"
    return {
        "status": "OK" if not missing else "PARTIAL",
        "as_of_date": as_of_date,
        "values": values,
        "history": history,
        "trend": _share_structure_trend(history),
        "metadata": {
            "freshness": "LATEST_DISCLOSED_SHARE_STRUCTURE",
            "realtime": False,
            "quality": quality,
            "quality_flags": [f"MISSING_{name.upper()}" for name in missing],
            "date_semantics": "SHARE_STRUCTURE_EFFECTIVE_OR_DISCLOSED_DATE",
            "history_period_limit": SHARE_STRUCTURE_HISTORY_LIMIT,
            "trend_policy": "latest disclosed structure versus immediately previous dated structure",
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


def _normalize_relationship_rows(rows):
    normalized = []
    if not isinstance(rows, list):
        return normalized
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("HOLDER_NAME") or "").strip()
        if not name:
            continue
        normalized.append(
            {
                "name": name,
                "hold_ratio_percent": _as_float(row.get("HOLD_RATIO")),
                "as_of_date": _date(
                    row.get("END_DATE") or row.get("REPORT_DATE") or row.get("CHANGE_DATE")
                ),
            }
        )
    return normalized


def _common_explicit_date(rows):
    dates = sorted({row.get("as_of_date") for row in rows if row.get("as_of_date")})
    return dates[-1] if dates else None


def _current_relationship_rows(rows):
    latest = _common_explicit_date(rows)
    if latest:
        return [row for row in rows if row.get("as_of_date") == latest]
    return list(rows)


def _relationship_history(rows):
    grouped = {}
    for row in rows:
        if row.get("as_of_date"):
            grouped.setdefault(row["as_of_date"], []).append(row)
    return [
        {"as_of_date": key, "holders": grouped[key]}
        for key in sorted(grouped, reverse=True)
    ]


def _control_change(actual_history, controlling_history):
    history = actual_history if len(actual_history) >= 2 else controlling_history
    if len(history) < 2:
        return {
            "state": "UNKNOWN",
            "changed": None,
            "latest_as_of_date": history[0]["as_of_date"] if history else None,
            "baseline_as_of_date": None,
            "before_names": [],
            "after_names": [],
            "evidence_policy": "TWO_DATED_PROVIDER_DECLARED_RELATIONSHIP_PERIODS_REQUIRED",
        }
    latest, previous = history[:2]
    after_names = sorted({item["name"] for item in latest["holders"]})
    before_names = sorted({item["name"] for item in previous["holders"]})
    return {
        "state": "CHANGED" if before_names != after_names else "UNCHANGED",
        "changed": before_names != after_names,
        "latest_as_of_date": latest["as_of_date"],
        "baseline_as_of_date": previous["as_of_date"],
        "before_names": before_names,
        "after_names": after_names,
        "evidence_policy": "TWO_DATED_PROVIDER_DECLARED_RELATIONSHIP_PERIODS_REQUIRED",
    }


def _concert_party_aggregate(payload):
    rows = []
    for section in ("sjkzr", "kggd"):
        values = (payload or {}).get(section) or []
        if not isinstance(values, list):
            continue
        for row in values:
            if not isinstance(row, dict):
                continue
            marker = str(
                row.get("IS_CONCERT_PARTY")
                or row.get("CONCERT_PARTY")
                or row.get("RELATION_TYPE")
                or ""
            ).strip()
            if marker.upper() not in {"1", "TRUE", "YES", "Y"} and "一致行动" not in marker:
                continue
            name = str(row.get("HOLDER_NAME") or "").strip()
            if name:
                rows.append(
                    {
                        "name": name,
                        "hold_ratio_percent": _as_float(row.get("HOLD_RATIO")),
                        "as_of_date": _date(
                            row.get("END_DATE") or row.get("REPORT_DATE") or row.get("CHANGE_DATE")
                        ),
                    }
                )
    rows = _current_relationship_rows(rows)
    ratios = [row.get("hold_ratio_percent") for row in rows]
    complete = bool(rows) and all(value is not None for value in ratios)
    return {
        "status": "OK" if complete else "PARTIAL" if rows else "UNAVAILABLE",
        "holders": rows,
        "aggregate_hold_ratio_percent": _round(sum(ratios), 4) if complete else None,
        "as_of_date": max((row.get("as_of_date") for row in rows if row.get("as_of_date")), default=None),
        "evidence_policy": "EXPLICIT_PROVIDER_CONCERT_PARTY_MARKER_ONLY; NEVER_INFER_FROM_NAMES",
    }


def normalize_controllers(payload, source_url, provider_code, fetched_at):
    actual_all_rows = _normalize_relationship_rows((payload or {}).get("sjkzr"))
    controlling_all_rows = _normalize_relationship_rows((payload or {}).get("kggd"))
    actual_rows = _current_relationship_rows(actual_all_rows)
    controlling_rows = _current_relationship_rows(controlling_all_rows)
    actual_date = _common_explicit_date(actual_rows)
    controlling_date = _common_explicit_date(controlling_rows)
    actual_history = _relationship_history(actual_all_rows)
    controlling_history = _relationship_history(controlling_all_rows)

    quality_flags = []
    if not actual_rows:
        quality_flags.append("ACTUAL_CONTROLLER_UNAVAILABLE")
    elif actual_date is None:
        quality_flags.append("ACTUAL_CONTROLLER_UNDATED")
    if not controlling_rows:
        quality_flags.append("CONTROLLING_SHAREHOLDER_NOT_IDENTIFIED_BY_PROVIDER")
    elif controlling_date is None:
        quality_flags.append("CONTROLLING_SHAREHOLDER_UNDATED")

    if actual_rows and controlling_rows:
        status = "OK"
        quality = "PASS" if not quality_flags else "PARTIAL"
    elif actual_rows or controlling_rows:
        status = "PARTIAL"
        quality = "PARTIAL"
    else:
        status = "UNAVAILABLE"
        quality = "FAILED"

    explicit_dates = [value for value in (actual_date, controlling_date) if value]
    return {
        "status": status,
        "as_of_date": max(explicit_dates) if explicit_dates else None,
        "actual_controller": {
            "status": "OK" if actual_rows else "UNAVAILABLE",
            "holders": actual_rows,
            "as_of_date": actual_date,
            "history": actual_history,
        },
        "controlling_shareholder": {
            "status": "OK" if controlling_rows else "UNAVAILABLE",
            "holders": controlling_rows,
            "as_of_date": controlling_date,
            "inference_policy": "PROVIDER_DECLARED_ONLY; NEVER_INFER_FROM_LARGEST_HOLDER",
            "history": controlling_history,
        },
        "concert_party_aggregate": _concert_party_aggregate(payload),
        "control_change": _control_change(actual_history, controlling_history),
        "recent_holding_changes": [],
        "metadata": {
            "freshness": (
                "CURRENT_PROVIDER_RELATIONSHIP"
                if actual_rows or controlling_rows
                else "UNAVAILABLE"
            ),
            "realtime": False,
            "quality": quality,
            "quality_flags": quality_flags,
            "date_semantics": (
                "RELATIONSHIP_DATE_IF_PROVIDER_EXPOSES_ONE; "
                "UNDATED_PROVIDER_CURRENT_STATE_OTHERWISE"
            ),
        },
        "provenance": {
            "provider": "Eastmoney",
            "source_tier": "PRIMARY_PROVIDER",
            "endpoint": SHAREHOLDER_RESEARCH_ENDPOINT,
            "source_url": source_url,
            "provider_code": provider_code,
            "fetched_at": fetched_at,
            "field_mapping": {
                "actual_controller": "sjkzr[].HOLDER_NAME/HOLD_RATIO",
                "controlling_shareholder": "kggd[].HOLDER_NAME/HOLD_RATIO when provider exposes kggd",
                "concert_party_aggregate": "explicit provider concert-party marker only",
            },
            "non_inference_guarantee": (
                "sdgd rank-1/top-holder rows are not promoted to controlling_shareholder"
            ),
        },
    }


def _unavailable_section(endpoint, fetched_at, error):
    return {
        "status": "UNAVAILABLE",
        "as_of_date": None,
        "metadata": {
            "freshness": "UNAVAILABLE",
            "realtime": False,
            "quality": "FAILED",
            "quality_flags": ["PROVIDER_ERROR"],
            "error": error,
        },
        "provenance": {
            "provider": "Eastmoney",
            "source_tier": "PRIMARY_PROVIDER",
            "endpoint": endpoint,
            "source_url": None,
            "provider_code": None,
            "fetched_at": fetched_at,
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
            "history": [],
            "trend": _share_structure_trend([]),
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
        "controllers": {
            "status": "DEFERRED",
            "as_of_date": None,
            "actual_controller": {"status": "DEFERRED", "holders": [], "as_of_date": None, "history": []},
            "controlling_shareholder": {
                "status": "DEFERRED",
                "holders": [],
                "as_of_date": None,
                "inference_policy": "PROVIDER_DECLARED_ONLY; NEVER_INFER_FROM_LARGEST_HOLDER",
                "history": [],
            },
            "concert_party_aggregate": {
                "status": "DEFERRED", "holders": [], "aggregate_hold_ratio_percent": None,
                "as_of_date": None,
                "evidence_policy": "EXPLICIT_PROVIDER_CONCERT_PARTY_MARKER_ONLY; NEVER_INFER_FROM_NAMES",
            },
            "control_change": _control_change([], []),
            "recent_holding_changes": [],
            "metadata": {
                "freshness": "NOT_FETCHED_IN_INTRADAY_FAST",
                "realtime": False,
                "quality": "PARTIAL",
                "quality_flags": ["FULL_ONLY_CONTROLLERS_SLICE"],
            },
            "provenance": {
                "provider": "Eastmoney",
                "source_tier": "PRIMARY_PROVIDER",
                "endpoint": SHAREHOLDER_RESEARCH_ENDPOINT,
                "source_url": None,
                "provider_code": None,
                "fetched_at": fetched_at,
            },
        },
    }


def _overall_status(sections):
    statuses = [section.get("status") for section in sections]
    if statuses and all(status == "OK" for status in statuses):
        return "OK"
    if any(status in ("OK", "PARTIAL") for status in statuses):
        return "PARTIAL"
    return "UNAVAILABLE"


def _fetch_one(base, code, fetched_at):
    try:
        payload, url, provider_code = fetch_share_structure(base, code)
        share_structure = normalize_share_structure(payload, url, provider_code, fetched_at)
    except Exception as exc:
        share_structure = _unavailable_section(
            CAPITAL_STRUCTURE_ENDPOINT,
            fetched_at,
            f"{type(exc).__name__}: {exc}",
        )
        share_structure["values"] = None
        share_structure["history"] = []
        share_structure["trend"] = _share_structure_trend([])

    try:
        payload, url, provider_code = fetch_controllers(base, code)
        controllers = normalize_controllers(payload, url, provider_code, fetched_at)
    except Exception as exc:
        controllers = _unavailable_section(
            SHAREHOLDER_RESEARCH_ENDPOINT,
            fetched_at,
            f"{type(exc).__name__}: {exc}",
        )
        controllers["actual_controller"] = {
            "status": "UNAVAILABLE",
            "holders": [],
            "as_of_date": None,
            "history": [],
        }
        controllers["controlling_shareholder"] = {
            "status": "UNAVAILABLE",
            "holders": [],
            "as_of_date": None,
            "inference_policy": "PROVIDER_DECLARED_ONLY; NEVER_INFER_FROM_LARGEST_HOLDER",
            "history": [],
        }
        controllers["concert_party_aggregate"] = _concert_party_aggregate({})
        controllers["control_change"] = _control_change([], [])
        controllers["recent_holding_changes"] = []

    return code, {
        "version": OWNERSHIP_CAPITAL_VERSION,
        "status": _overall_status((share_structure, controllers)),
        "share_structure": share_structure,
        "controllers": controllers,
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
        controllers = context.get("controllers") or {}
        actual = (controllers.get("actual_controller") or {}).get("holders") or []
        controlling = (controllers.get("controlling_shareholder") or {}).get("holders") or []
        print(
            "OWNERSHIP_CAPITAL "
            f"{code} status={context.get('status')} "
            f"share_status={share.get('status')} as_of={share.get('as_of_date')} "
            f"total={values.get('total_shares')} float_a={values.get('float_shares')} "
            f"restricted={values.get('restricted_shares')} "
            f"controllers_status={controllers.get('status')} "
            f"actual_controller_count={len(actual)} controlling_holder_count={len(controlling)}",
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
        "implemented_sections": ["share_structure", "controllers"],
        "share_structure_contract": (
            "DATED_PROVIDER_ROW; FLOAT_SHARES=LISTED_A_SHARES; RATIOS_DERIVED"
        ),
        "controllers_contract": (
            "ACTUAL_CONTROLLER=sjkzr; CONTROLLING_SHAREHOLDER=PROVIDER_DECLARED_kggd_ONLY; "
            "NEVER_INFER_FROM_TOP_HOLDER"
        ),
        "intraday_fast_policy": "DEFER_NETWORK_UNTIL_CACHE_CONTINUITY_SLICE",
    }
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"SNAPSHOT_SCHEMA_UPGRADED schema_version={SNAPSHOT_SCHEMA_VERSION} "
        f"feature=ownership_and_capital:{OWNERSHIP_CAPITAL_VERSION}",
        flush=True,
    )
