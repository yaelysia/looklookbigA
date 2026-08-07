import json
import statistics
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
CONFIG_PATH = Path("config/quote_watchlist.json")
SNAPSHOT_PATH = Path("snapshot.json")

QUOTE_FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f71,f86,f169,f170,f171"

INDICES = [
    ("上证指数", "1.000001"),
    ("深证成指", "0.399001"),
    ("创业板指", "0.399006"),
]


def http_get(url: str, timeout: int = 8, attempts: int = 3) -> str:
    last_error = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150 Safari/537.36",
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://quote.eastmoney.com/",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.5 * attempt)
    raise last_error


def normalize_code(code) -> str:
    code = str(code).strip()
    if not code.isdigit() or len(code) > 6:
        raise ValueError(f"invalid stock code: {code!r}")
    return code.zfill(6)


def infer_identifiers(code: str):
    code = normalize_code(code)
    if code.startswith(("4", "8", "92")):
        return "BJ", f"0.{code}", f"bj{code}"
    if code.startswith(("6", "68", "69")):
        return "SH", f"1.{code}", f"sh{code}"
    if code.startswith(("0", "3")):
        return "SZ", f"0.{code}", f"sz{code}"
    raise ValueError(f"unsupported A-share code pattern: {code}")


def load_config():
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    detail = []
    standalone_light = []
    groups = {}

    for code in raw.get("detail_codes", []):
        c = normalize_code(code)
        if c not in detail:
            detail.append(c)

    for code in raw.get("light_codes", []):
        c = normalize_code(code)
        if c not in detail and c not in standalone_light:
            standalone_light.append(c)

    group_light_candidates = []
    raw_groups = raw.get("groups", {}) or {}
    if not isinstance(raw_groups, dict):
        raise ValueError("groups must be an object")

    for group_id, group in raw_groups.items():
        if not isinstance(group, dict):
            raise ValueError(f"group {group_id!r} must be an object")
        target = normalize_code(group["target_code"]) if group.get("target_code") else None
        members = []
        for code in group.get("member_codes", []):
            c = normalize_code(code)
            if c != target and c not in members:
                members.append(c)
            if c not in detail and c not in group_light_candidates:
                group_light_candidates.append(c)
        if target and target not in detail and target not in group_light_candidates:
            group_light_candidates.append(target)
        groups[group_id] = {
            "label": str(group.get("label") or group_id),
            "target_code": target,
            "member_codes": members,
        }

    light = []
    for code in group_light_candidates + standalone_light:
        if code not in detail and code not in light:
            light.append(code)

    max_total = int(raw.get("max_total_codes", 50))
    max_total = max(1, min(max_total, 100))
    allowed_light = max(0, max_total - len(detail))
    truncated = len(light) > allowed_light
    light = light[:allowed_light]
    active_codes = set(detail) | set(light)

    for group in groups.values():
        group["active_member_codes"] = [c for c in group["member_codes"] if c in active_codes]
        group["members_truncated"] = len(group["active_member_codes"]) != len(group["member_codes"])

    return {
        "detail_codes": detail,
        "light_codes": light,
        "groups": groups,
        "max_total_codes": max_total,
        "truncated": truncated,
    }


def market_dt(ts):
    try:
        return datetime.fromtimestamp(float(ts), CST)
    except Exception:
        return None


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def in_market_window(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 25 <= hm <= 11 * 60 + 35) or (12 * 60 + 55 <= hm <= 15 * 60 + 5)


def freshness(now: datetime, quote_dt):
    if not quote_dt:
        return "UNKNOWN", None
    lag = max(0, int((now - quote_dt).total_seconds()))
    same_day = quote_dt.date() == now.date()
    if in_market_window(now):
        return ("LIVE" if same_day and lag <= 180 else "STALE"), lag
    return ("CURRENT_SESSION" if same_day else "LAST_SESSION"), lag


def eastmoney_quote(secid: str):
    params = {
        "secid": secid,
        "fields": QUOTE_FIELDS,
        "invt": "2",
        "fltt": "2",
        "_": str(int(time.time() * 1000)),
    }
    url = "https://push2.eastmoney.com/api/qt/stock/get?" + urllib.parse.urlencode(params)
    payload = json.loads(http_get(url))
    return payload.get("data")


def tencent_minutes(tcode: str):
    params = {"code": tcode, "_t": str(int(time.time() * 1000))}
    url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?" + urllib.parse.urlencode(params)
    obj = json.loads(http_get(url))
    node = obj.get("data", {}).get(tcode, {})
    data = node.get("data", {})
    return data.get("date"), data.get("data") or []


def quote_payload(now: datetime, code: str):
    market, secid, _ = infer_identifiers(code)
    d = eastmoney_quote(secid)
    if not d:
        raise RuntimeError("Eastmoney returned no data")
    qdt = market_dt(d.get("f86"))
    state, lag = freshness(now, qdt)
    amount_raw = float(d.get("f48") or 0)
    return {
        "code": code,
        "name": d.get("f58") or code,
        "market": market,
        "source": "Eastmoney",
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
        "amount_1e8": round(amount_raw / 1e8, 4),
        "market_time_cst": fmt_dt(qdt),
        "lag_seconds": lag,
        "freshness": state,
    }


def parse_minutes(rows):
    parsed = []
    prev_vol = 0.0
    prev_amount = 0.0
    for row in rows:
        parts = str(row).split()
        if len(parts) < 4:
            continue
        try:
            t = parts[0]
            price = float(parts[1])
            cum_vol = float(parts[2])
            cum_amount = float(parts[3])
            parsed.append(
                {
                    "time": t,
                    "price": price,
                    "cum_volume": cum_vol,
                    "cum_amount": cum_amount,
                    "delta_volume": max(0.0, cum_vol - prev_vol),
                    "delta_amount": max(0.0, cum_amount - prev_amount),
                }
            )
            prev_vol = cum_vol
            prev_amount = cum_amount
        except ValueError:
            continue
    return parsed


def pct_change(a, b):
    if not a or not b:
        return None
    return (a / b - 1.0) * 100.0


def detail_payload(now: datetime, code: str):
    market, _, tcode = infer_identifiers(code)
    result = {
        "code": code,
        "market": market,
        "quote": None,
        "minutes": None,
        "status": "FAILED",
        "errors": [],
    }

    try:
        result["quote"] = quote_payload(now, code)
    except Exception as exc:
        result["errors"].append(f"quote: {type(exc).__name__}: {exc}")

    try:
        date, rows = tencent_minutes(tcode)
        mins = parse_minutes(rows)
        today = now.strftime("%Y%m%d")
        last_price = mins[-1]["price"] if mins else None
        p5 = mins[-6]["price"] if len(mins) >= 6 else (mins[0]["price"] if mins else None)
        p15 = mins[-16]["price"] if len(mins) >= 16 else (mins[0]["price"] if mins else None)
        result["minutes"] = {
            "source": "Tencent",
            "date": date,
            "freshness": "LIVE" if date == today else "STALE",
            "count": len(mins),
            "last_time": mins[-1]["time"] if mins else None,
            "last_price": last_price,
            "trend_5m_percent": pct_change(last_price, p5),
            "trend_15m_percent": pct_change(last_price, p15),
            "first_10": mins[:10],
            "last_15": mins[-15:],
        }
    except Exception as exc:
        result["errors"].append(f"minutes: {type(exc).__name__}: {exc}")

    if result["quote"] and result["minutes"] and result["minutes"]["freshness"] == "LIVE":
        result["status"] = "OK"
    elif result["quote"] or result["minutes"]:
        result["status"] = "PARTIAL"
    return result


def light_payload(now: datetime, code: str):
    try:
        return {"status": "OK", "quote": quote_payload(now, code), "error": None}
    except Exception as exc:
        return {"status": "ERROR", "quote": None, "error": f"{type(exc).__name__}: {exc}"}


def fetch_light_group(now: datetime, codes):
    results = {}
    if not codes:
        return results
    workers = min(4, len(codes))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(light_payload, now, code): code for code in codes}
        for future in as_completed(future_map):
            code = future_map[future]
            try:
                results[code] = future.result()
            except Exception as exc:
                results[code] = {"status": "ERROR", "quote": None, "error": str(exc)}
    return dict(sorted(results.items()))


def fetch_indices(now: datetime):
    out = {}
    for name, secid in INDICES:
        try:
            d = eastmoney_quote(secid)
            if not d:
                raise RuntimeError("no data")
            qdt = market_dt(d.get("f86"))
            state, lag = freshness(now, qdt)
            out[name] = {
                "status": "OK",
                "quote": {
                    "latest": d.get("f43"),
                    "change_percent": d.get("f170"),
                    "open": d.get("f46"),
                    "high": d.get("f44"),
                    "low": d.get("f45"),
                    "market_time_cst": fmt_dt(qdt),
                    "lag_seconds": lag,
                    "freshness": state,
                },
                "error": None,
            }
        except Exception as exc:
            out[name] = {"status": "ERROR", "quote": None, "error": f"{type(exc).__name__}: {exc}"}
    return out


def safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def quote_for_code(code, detail, light):
    item = detail.get(code)
    if item and item.get("quote"):
        return item["quote"]
    item = light.get(code)
    if item and item.get("quote"):
        return item["quote"]
    return None


def build_group_summary(group_id, group, detail, light):
    members = []
    for code in group.get("active_member_codes", []):
        q = quote_for_code(code, detail, light)
        pct = safe_float(q.get("change_percent")) if q else None
        members.append(
            {
                "code": code,
                "name": q.get("name") if q else None,
                "latest": q.get("latest") if q else None,
                "change_percent": pct,
                "amount_1e8": q.get("amount_1e8") if q else None,
                "freshness": q.get("freshness") if q else None,
                "available": pct is not None,
            }
        )

    available = [m for m in members if m["available"]]
    pcts = [m["change_percent"] for m in available]
    mean_pct = statistics.fmean(pcts) if pcts else None
    median_pct = statistics.median(pcts) if pcts else None
    up = sum(1 for x in pcts if x > 0)
    down = sum(1 for x in pcts if x < 0)
    flat = len(pcts) - up - down

    target_code = group.get("target_code")
    target_quote = quote_for_code(target_code, detail, light) if target_code else None
    target_pct = safe_float(target_quote.get("change_percent")) if target_quote else None

    ranked = sorted(available, key=lambda x: x["change_percent"], reverse=True)
    requested_count = len(group.get("member_codes", []))
    coverage = len(available) / requested_count if requested_count else 1.0
    status = "OK" if coverage >= 0.75 else ("PARTIAL" if available else "ERROR")

    return {
        "group_id": group_id,
        "label": group.get("label") or group_id,
        "status": status,
        "target": {
            "code": target_code,
            "name": target_quote.get("name") if target_quote else None,
            "change_percent": target_pct,
        },
        "requested_member_count": requested_count,
        "active_member_count": len(group.get("active_member_codes", [])),
        "covered_member_count": len(available),
        "coverage_percent": round(coverage * 100, 2),
        "mean_change_percent": round(mean_pct, 4) if mean_pct is not None else None,
        "median_change_percent": round(median_pct, 4) if median_pct is not None else None,
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "breadth_score_percent": round((up - down) / len(pcts) * 100, 2) if pcts else None,
        "target_vs_peer_mean_percent": round(target_pct - mean_pct, 4)
        if target_pct is not None and mean_pct is not None
        else None,
        "target_vs_peer_median_percent": round(target_pct - median_pct, 4)
        if target_pct is not None and median_pct is not None
        else None,
        "leaders": ranked[:3],
        "laggards": list(reversed(ranked[-3:])),
        "members": members,
    }


def main():
    started = time.monotonic()
    now = datetime.now(CST)
    cfg = load_config()

    print("REALTIME_A_SHARE_WATCHLIST_V2", flush=True)
    print(
        f"RUNNER_CST {now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} "
        f"detail={len(cfg['detail_codes'])} light={len(cfg['light_codes'])} "
        f"groups={len(cfg['groups'])} max_total={cfg['max_total_codes']} "
        f"truncated={cfg['truncated']}",
        flush=True,
    )

    detail = {}
    for code in cfg["detail_codes"]:
        item = detail_payload(now, code)
        detail[code] = item
        q = item.get("quote") or {}
        m = item.get("minutes") or {}
        print(
            f"DETAIL {code} {q.get('name', code)} status={item['status']} "
            f"latest={q.get('latest')} pct={q.get('change_percent')}% "
            f"high={q.get('high')} low={q.get('low')} "
            f"quote_time={q.get('market_time_cst')} quote_live={q.get('freshness')} "
            f"minute_last={m.get('last_time')}:{m.get('last_price')}",
            flush=True,
        )

    light = fetch_light_group(now, cfg["light_codes"])
    ok_light = sum(1 for x in light.values() if x.get("status") == "OK")
    print(f"LIGHT status={ok_light}/{len(light)} ok", flush=True)

    groups = {}
    for group_id, group in cfg["groups"].items():
        summary = build_group_summary(group_id, group, detail, light)
        groups[group_id] = summary
        target = summary["target"]
        print(
            f"GROUP {group_id} status={summary['status']} "
            f"coverage={summary['covered_member_count']}/{summary['requested_member_count']} "
            f"mean={summary['mean_change_percent']}% median={summary['median_change_percent']}% "
            f"up/down/flat={summary['up_count']}/{summary['down_count']}/{summary['flat_count']} "
            f"target={target['code']}:{target['change_percent']}% "
            f"vs_mean={summary['target_vs_peer_mean_percent']}%",
            flush=True,
        )

    indices = fetch_indices(now)

    snapshot = {
        "schema_version": 3,
        "runner_time_cst": now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "runner_time_utc": now.astimezone(timezone.utc).isoformat(),
        "market_window": in_market_window(now),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "config": cfg,
        "detail_stocks": detail,
        "light_stocks": light,
        "groups": groups,
        "indices": indices,
    }

    SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"SNAPSHOT_WRITTEN {SNAPSHOT_PATH} bytes={SNAPSHOT_PATH.stat().st_size} "
        f"elapsed={snapshot['elapsed_seconds']}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
