import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


PRIMARY_SOURCE = "Eastmoney"
FALLBACK_SOURCE = "Tencent"
CONSISTENT_GAP_PERCENT = 0.08
DIVERGENT_GAP_PERCENT = 0.35
TRANSPORT_POLICY = {
    "https_only": True,
    "plaintext_http_fallback": False,
}

INDEX_TENCENT_CODES = {
    "上证指数": "sh000001",
    "深证成指": "sz399001",
    "创业板指": "sz399006",
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


def _market_from_tcode(tcode):
    return "SH" if str(tcode).lower().startswith("sh") else "SZ"


def _parse_tencent_time(base, value):
    value = str(value or "").strip()
    try:
        if len(value) >= 14 and value[:14].isdigit():
            return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=base.CST)
    except ValueError:
        pass
    return None


def _fetch_tencent_text(tcodes):
    joined = ",".join(tcodes)
    url = "https://qt.gtimg.cn/q=" + joined
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://gu.qq.com/",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=7) as resp:
        return resp.read().decode("gbk", errors="replace")


def _parse_tencent_records(base, now, text):
    records = {}
    for match in re.finditer(r'v_([a-z]{2}\d{6})="([^"]*)"', text, flags=re.I):
        tcode = match.group(1).lower()
        fields = match.group(2).split("~")
        if len(fields) < 35:
            continue

        latest = _as_float(fields[3])
        previous_close = _as_float(fields[4])
        open_price = _as_float(fields[5])
        change = _as_float(fields[31]) if len(fields) > 31 else None
        change_pct = _as_float(fields[32]) if len(fields) > 32 else None
        high = _as_float(fields[33]) if len(fields) > 33 else None
        low = _as_float(fields[34]) if len(fields) > 34 else None
        volume_raw = _as_float(fields[36]) if len(fields) > 36 else _as_float(fields[6])
        amount_wan = _as_float(fields[37]) if len(fields) > 37 else None

        if change is None and latest is not None and previous_close is not None:
            change = latest - previous_close
        if change_pct is None and latest is not None and previous_close not in (None, 0):
            change_pct = (latest / previous_close - 1.0) * 100.0
        amplitude = None
        if high is not None and low is not None and previous_close not in (None, 0):
            amplitude = (high - low) / previous_close * 100.0

        qdt = _parse_tencent_time(base, fields[30] if len(fields) > 30 else None)
        state, lag = base.freshness(now, qdt)
        amount_raw = amount_wan * 10000.0 if amount_wan is not None else None
        code = fields[2].zfill(6) if len(fields) > 2 and fields[2].isdigit() else tcode[-6:]

        records[tcode] = {
            "code": code,
            "name": fields[1] if len(fields) > 1 else code,
            "market": _market_from_tcode(tcode),
            "source": FALLBACK_SOURCE,
            "latest": latest,
            "open": open_price,
            "high": high,
            "low": low,
            "average": None,
            "previous_close": previous_close,
            "change": change,
            "change_percent": change_pct,
            "amplitude_percent": amplitude,
            "volume_raw": volume_raw,
            "amount_raw": amount_raw,
            "amount_1e8": _round(amount_wan / 10000.0, 4) if amount_wan is not None else None,
            "market_time_cst": base.fmt_dt(qdt),
            "lag_seconds": lag,
            "freshness": state,
        }
    return records


def tencent_batch_quotes(base, now, codes):
    code_to_tcode = {}
    for code in codes:
        _, _, tcode = base.infer_identifiers(code)
        code_to_tcode[code] = tcode.lower()
    if not code_to_tcode:
        return {}

    records = _parse_tencent_records(base, now, _fetch_tencent_text(list(code_to_tcode.values())))
    out = {}
    for code, tcode in code_to_tcode.items():
        quote = records.get(tcode)
        if quote:
            out[code] = quote
    return out


def _eastmoney_data(base, secid):
    params = {
        "secid": secid,
        "fields": base.QUOTE_FIELDS,
        "invt": "2",
        "fltt": "2",
        "_": str(int(time.time() * 1000)),
    }
    url = "https://push2.eastmoney.com/api/qt/stock/get?" + urllib.parse.urlencode(params)
    payload = json.loads(base.http_get(url, timeout=6, attempts=2))
    data = payload.get("data")
    if not data:
        raise RuntimeError("Eastmoney returned no data")
    return data


def _eastmoney_stock_quote(base, now, code):
    market, secid, _ = base.infer_identifiers(code)
    d = _eastmoney_data(base, secid)
    qdt = base.market_dt(d.get("f86"))
    state, lag = base.freshness(now, qdt)
    amount_raw = _as_float(d.get("f48")) or 0.0
    return {
        "code": code,
        "name": d.get("f58") or code,
        "market": market,
        "source": PRIMARY_SOURCE,
        "latest": d.get("f43"),
        "open": d.get("f46"),
        "high": d.get("f44"),
        "low": d.get("f45"),
        "average": d.get("f71"),
        "previous_close": d.get("f60"),
        "change": d.get("f169"),
        "change_percent": d.get("f170"),
        "amplitude_percent": d.get("f171"),
        "volume_raw": d.get("f47"),
        "amount_raw": amount_raw,
        "amount_1e8": _round(amount_raw / 1e8, 4),
        "market_time_cst": base.fmt_dt(qdt),
        "lag_seconds": lag,
        "freshness": state,
    }


def _eastmoney_index_quote(base, now, secid):
    d = _eastmoney_data(base, secid)
    qdt = base.market_dt(d.get("f86"))
    state, lag = base.freshness(now, qdt)
    return {
        "source": PRIMARY_SOURCE,
        "latest": d.get("f43"),
        "change_percent": d.get("f170"),
        "open": d.get("f46"),
        "high": d.get("f44"),
        "low": d.get("f45"),
        "market_time_cst": base.fmt_dt(qdt),
        "lag_seconds": lag,
        "freshness": state,
    }


def _is_usable(base, now, quote):
    if not isinstance(quote, dict) or _as_float(quote.get("latest")) is None:
        return False
    state = quote.get("freshness")
    if base.in_market_window(now):
        return state == "LIVE"
    return state in ("CURRENT_SESSION", "LAST_SESSION")


def _consensus(primary, fallback):
    p = _as_float((primary or {}).get("latest"))
    f = _as_float((fallback or {}).get("latest"))
    if p is None or f is None:
        return {
            "status": "SINGLE_SOURCE",
            "price_gap": None,
            "price_gap_percent": None,
        }

    gap = abs(p - f)
    midpoint = (abs(p) + abs(f)) / 2.0
    gap_pct = gap / midpoint * 100.0 if midpoint else 0.0
    if gap_pct <= CONSISTENT_GAP_PERCENT:
        status = "CONSISTENT"
    elif gap_pct <= DIVERGENT_GAP_PERCENT:
        status = "NEAR"
    else:
        status = "DIVERGENT"
    return {
        "status": status,
        "price_gap": _round(gap, 4),
        "price_gap_percent": _round(gap_pct, 4),
    }


def _provider_state(base, now, quote, error):
    return {
        "status": "ERROR" if error else ("OK" if quote else "MISSING"),
        "usable": _is_usable(base, now, quote),
        "latest": (quote or {}).get("latest"),
        "market_time_cst": (quote or {}).get("market_time_cst"),
        "lag_seconds": (quote or {}).get("lag_seconds"),
        "freshness": (quote or {}).get("freshness"),
        "error": error,
    }


def _select_quote(base, now, primary, fallback, primary_error=None, fallback_error=None):
    primary_ok = _is_usable(base, now, primary)
    fallback_ok = _is_usable(base, now, fallback)
    consensus = _consensus(primary, fallback)

    if primary_ok and fallback_ok:
        if consensus["status"] == "DIVERGENT":
            p_lag = (primary or {}).get("lag_seconds")
            f_lag = (fallback or {}).get("lag_seconds")
            if f_lag is not None and (p_lag is None or f_lag < p_lag):
                selected = fallback
                reason = "BOTH_USABLE_DIVERGENT_FALLBACK_NEWER"
            else:
                selected = primary
                reason = "BOTH_USABLE_DIVERGENT_PRIMARY_NEWER_OR_EQUAL"
        else:
            selected = primary
            reason = "PRIMARY_USABLE"
    elif primary_ok:
        selected = primary
        reason = "PRIMARY_USABLE_FALLBACK_UNUSABLE"
    elif fallback_ok:
        selected = fallback
        reason = "PRIMARY_UNUSABLE_FALLBACK_USABLE"
    else:
        candidates = [q for q in (primary, fallback) if isinstance(q, dict) and _as_float(q.get("latest")) is not None]
        if candidates:
            candidates.sort(key=lambda q: q.get("lag_seconds") if q.get("lag_seconds") is not None else 10**9)
            selected = candidates[0]
            reason = "NO_USABLE_SOURCE_RETURNING_STALE_FOR_DIAGNOSTICS"
        else:
            raise RuntimeError(
                "no quote source available: "
                f"Eastmoney={primary_error or 'missing'}; Tencent={fallback_error or 'missing'}"
            )

    selected = dict(selected)
    selected["resilience"] = {
        "primary_source": PRIMARY_SOURCE,
        "fallback_source": FALLBACK_SOURCE,
        "selected_source": selected.get("source"),
        "fallback_used": selected.get("source") == FALLBACK_SOURCE,
        "selection_reason": reason,
        "consensus": consensus,
        "providers": {
            PRIMARY_SOURCE: _provider_state(base, now, primary, primary_error),
            FALLBACK_SOURCE: _provider_state(base, now, fallback, fallback_error),
        },
    }
    return selected


def resilient_stock_quote(base, now, code):
    primary = None
    fallback = None
    primary_error = None
    fallback_error = None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(_eastmoney_stock_quote, base, now, code): PRIMARY_SOURCE,
            pool.submit(tencent_batch_quotes, base, now, [code]): FALLBACK_SOURCE,
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                value = future.result()
                if source == PRIMARY_SOURCE:
                    primary = value
                else:
                    fallback = value.get(code)
                    if fallback is None:
                        fallback_error = "Tencent returned no matching quote"
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                if source == PRIMARY_SOURCE:
                    primary_error = msg
                else:
                    fallback_error = msg

    return _select_quote(base, now, primary, fallback, primary_error, fallback_error)


def _tencent_index_quotes(base, now):
    tcodes = list(INDEX_TENCENT_CODES.values())
    records = _parse_tencent_records(base, now, _fetch_tencent_text(tcodes))
    out = {}
    for name, tcode in INDEX_TENCENT_CODES.items():
        quote = records.get(tcode)
        if quote:
            out[name] = {
                "source": FALLBACK_SOURCE,
                "latest": quote.get("latest"),
                "change_percent": quote.get("change_percent"),
                "open": quote.get("open"),
                "high": quote.get("high"),
                "low": quote.get("low"),
                "market_time_cst": quote.get("market_time_cst"),
                "lag_seconds": quote.get("lag_seconds"),
                "freshness": quote.get("freshness"),
            }
    return out


def resilient_indices(base, now):
    primary_results = {}
    primary_errors = {}
    fallback_results = {}
    fallback_error = None

    workers = min(4, len(base.INDICES) + 1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(_eastmoney_index_quote, base, now, secid): ("primary", name)
            for name, secid in base.INDICES
        }
        future_map[pool.submit(_tencent_index_quotes, base, now)] = ("fallback", None)

        for future in as_completed(future_map):
            kind, name = future_map[future]
            try:
                value = future.result()
                if kind == "primary":
                    primary_results[name] = value
                else:
                    fallback_results = value
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                if kind == "primary":
                    primary_errors[name] = msg
                else:
                    fallback_error = msg

    out = {}
    for name, _ in base.INDICES:
        primary = primary_results.get(name)
        fallback = fallback_results.get(name)
        try:
            selected = _select_quote(
                base,
                now,
                primary,
                fallback,
                primary_errors.get(name),
                fallback_error if fallback is None else None,
            )
            status = "OK" if _is_usable(base, now, selected) else "PARTIAL"
            out[name] = {"status": status, "quote": selected, "error": None}
        except Exception as exc:
            out[name] = {"status": "ERROR", "quote": None, "error": f"{type(exc).__name__}: {exc}"}
    return out


def fetch_light_group_reliable(base, now, codes):
    if not codes:
        return {}

    results = {}
    batch_error = None
    try:
        batch = tencent_batch_quotes(base, now, codes)
        for code, quote in batch.items():
            results[code] = {"status": "OK", "quote": quote, "error": None}
    except Exception as exc:
        batch_error = f"{type(exc).__name__}: {exc}"

    missing = [code for code in codes if code not in results]
    recovered = 0
    for code in missing:
        retry = base.light_payload(now, code)
        results[code] = retry
        if retry.get("status") == "OK":
            recovered += 1
        time.sleep(0.12)

    ok = sum(1 for item in results.values() if item.get("status") == "OK")
    print(
        f"LIGHT_BATCH tencent={len(codes) - len(missing)}/{len(codes)} "
        f"fallback_recovered={recovered}/{len(missing)} final={ok}/{len(codes)} "
        f"batch_error={batch_error}",
        flush=True,
    )
    return dict(sorted(results.items()))


def install(base):
    def quote_payload(now, code):
        return resilient_stock_quote(base, now, code)

    def fetch_indices(now):
        return resilient_indices(base, now)

    def fetch_light_group(now, codes):
        return fetch_light_group_reliable(base, now, codes)

    base.quote_payload = quote_payload
    base.fetch_indices = fetch_indices
    base.fetch_light_group = fetch_light_group


def _collect_resilience(items):
    fallback = []
    divergent = []
    unavailable = []
    provider_counts = {}
    for key, item in (items or {}).items():
        quote = (item or {}).get("quote") or {}
        if not quote:
            unavailable.append(key)
            continue
        provider = quote.get("source") or "UNKNOWN"
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
        meta = quote.get("resilience") or {}
        if meta.get("fallback_used"):
            fallback.append(key)
        if ((meta.get("consensus") or {}).get("status")) == "DIVERGENT":
            divergent.append(key)
    return {
        "fallback_used": fallback,
        "divergent": divergent,
        "unavailable": unavailable,
        "provider_counts": provider_counts,
    }


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))

    detail = _collect_resilience(data.get("detail_stocks"))
    indices = _collect_resilience(data.get("indices"))
    light = _collect_resilience(data.get("light_stocks"))

    fallback_count = len(detail["fallback_used"]) + len(indices["fallback_used"]) + len(light["fallback_used"])
    divergent = detail["divergent"] + indices["divergent"] + light["divergent"]
    unavailable = detail["unavailable"] + indices["unavailable"] + light["unavailable"]

    if unavailable or divergent:
        status = "WARNING"
    elif fallback_count:
        status = "DEGRADED"
    else:
        status = "OK"

    data["schema_version"] = max(int(data.get("schema_version") or 0), 8)
    data.setdefault("features", {})["quote_resilience"] = "v1"
    data["quote_resilience"] = {
        "status": status,
        "primary_source": PRIMARY_SOURCE,
        "fallback_source": FALLBACK_SOURCE,
        "consensus_thresholds_percent": {
            "consistent_max": CONSISTENT_GAP_PERCENT,
            "divergent_above": DIVERGENT_GAP_PERCENT,
        },
        "detail": detail,
        "indices": indices,
        "light": light,
        "fallback_count": fallback_count,
        "divergent_count": len(divergent),
        "unavailable_count": len(unavailable),
        "minute_data_policy": {
            "primary_source": "Tencent",
            "fallback_source": None,
            "note": "Minute data remains single-source until a second provider is validated against the existing cumulative-volume semantics.",
        },
    }

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "QUOTE_RESILIENCE "
        f"status={status} fallback={fallback_count} divergent={len(divergent)} unavailable={len(unavailable)} "
        f"detail_providers={detail['provider_counts']} index_providers={indices['provider_counts']}",
        flush=True,
    )
    print("SNAPSHOT_SCHEMA_UPGRADED schema_version=8 feature=quote_resilience:v1", flush=True)
