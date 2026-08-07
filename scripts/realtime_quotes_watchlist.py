import json
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
    light = []

    for code in raw.get("detail_codes", []):
        c = normalize_code(code)
        if c not in detail:
            detail.append(c)

    for code in raw.get("light_codes", []):
        c = normalize_code(code)
        if c not in detail and c not in light:
            light.append(c)

    max_total = int(raw.get("max_total_codes", 50))
    max_total = max(1, min(max_total, 100))
    allowed_light = max(0, max_total - len(detail))
    truncated = len(light) > allowed_light
    light = light[:allowed_light]

    return {
        "detail_codes": detail,
        "light_codes": light,
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
    workers = min(6, len(codes))
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


def main():
    started = time.monotonic()
    now = datetime.now(CST)
    cfg = load_config()

    print("REALTIME_A_SHARE_WATCHLIST_V1", flush=True)
    print(
        f"RUNNER_CST {now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} "
        f"detail={len(cfg['detail_codes'])} light={len(cfg['light_codes'])} "
        f"max_total={cfg['max_total_codes']} truncated={cfg['truncated']}",
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

    indices = fetch_indices(now)

    snapshot = {
        "schema_version": 2,
        "runner_time_cst": now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "runner_time_utc": now.astimezone(timezone.utc).isoformat(),
        "market_window": in_market_window(now),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "config": cfg,
        "detail_stocks": detail,
        "light_stocks": light,
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
