import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
SNAPSHOT_PATH = Path("snapshot.json")

STOCKS = [
    ("002558", "0.002558", "sz002558", "巨人网络"),
    ("600795", "1.600795", "sh600795", "国电电力"),
]

INDICES = [
    ("上证指数", "1.000001"),
    ("深证成指", "0.399001"),
    ("创业板指", "0.399006"),
]

FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f71,f86,f169,f170,f171"


def http_get(url: str, timeout: int = 12, attempts: int = 3) -> str:
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
                time.sleep(0.8 * attempt)
    raise last_error


def eastmoney(secid: str):
    params = {
        "secid": secid,
        "fields": FIELDS,
        "invt": "2",
        "fltt": "2",
        "_": str(int(time.time() * 1000)),
    }
    url = "https://push2.eastmoney.com/api/qt/stock/get?" + urllib.parse.urlencode(params)
    payload = json.loads(http_get(url))
    return payload.get("data")


def tencent_minutes(code: str):
    params = {
        "code": code,
        "_t": str(int(time.time() * 1000)),
    }
    url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?" + urllib.parse.urlencode(params)
    obj = json.loads(http_get(url))
    node = obj.get("data", {}).get(code, {})
    data = node.get("data", {})
    return data.get("date"), data.get("data") or []


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


def fmt_pct(v):
    return "n/a" if v is None else f"{v:+.2f}%"


def print_minute_block(label, items):
    print(label)
    for item in items:
        print(
            f"  {item['time']} p={item['price']:.2f} "
            f"dvol={item['delta_volume']:.0f} damt={item['delta_amount'] / 1e6:.2f}m"
        )


def fetch_stock(now, code, secid, tcode, fallback_name):
    print(f"\n[{code} {fallback_name}]")
    result = {
        "code": code,
        "name": fallback_name,
        "quote": None,
        "minutes": None,
        "status": "FAILED_BOTH_SOURCES",
        "errors": [],
    }

    latest_ok = False
    try:
        d = eastmoney(secid)
        if not d:
            raise RuntimeError("Eastmoney returned no data")
        qdt = market_dt(d.get("f86"))
        state, lag = freshness(now, qdt)
        amount_raw = float(d.get("f48") or 0)
        result["name"] = d.get("f58") or fallback_name
        result["quote"] = {
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
        lag_text = "n/a" if lag is None else f"{lag}s"
        print(
            "QUOTE "
            f"latest={d.get('f43')} pct={d.get('f170')}% "
            f"open={d.get('f46')} high={d.get('f44')} low={d.get('f45')} "
            f"avg={d.get('f71')} prev={d.get('f60')} "
            f"amount={amount_raw / 1e8:.2f}e8 "
            f"time={fmt_dt(qdt)} lag={lag_text} freshness={state}"
        )
        latest_ok = True
    except Exception as exc:
        msg = f"QUOTE_ERROR {type(exc).__name__}: {exc}"
        result["errors"].append(msg)
        print(msg)

    minute_ok = False
    try:
        date, rows = tencent_minutes(tcode)
        mins = parse_minutes(rows)
        today = now.strftime("%Y%m%d")
        date_state = "LIVE" if date == today else "STALE"
        minute_payload = {
            "source": "Tencent",
            "date": date,
            "freshness": date_state,
            "count": len(mins),
            "last_time": None,
            "last_price": None,
            "trend_5m_percent": None,
            "trend_15m_percent": None,
            "first_10": mins[:10],
            "last_15": mins[-15:],
        }
        if mins:
            last_price = mins[-1]["price"]
            p5 = mins[-6]["price"] if len(mins) >= 6 else mins[0]["price"]
            p15 = mins[-16]["price"] if len(mins) >= 16 else mins[0]["price"]
            trend5 = pct_change(last_price, p5)
            trend15 = pct_change(last_price, p15)
            minute_payload.update(
                {
                    "last_time": mins[-1]["time"],
                    "last_price": last_price,
                    "trend_5m_percent": trend5,
                    "trend_15m_percent": trend15,
                }
            )
            print(
                f"MINUTES date={date} freshness={date_state} count={len(mins)} "
                f"last={mins[-1]['time']}:{last_price:.2f} "
                f"trend5={fmt_pct(trend5)} trend15={fmt_pct(trend15)}"
            )
            print_minute_block("FIRST10", mins[:10])
            print_minute_block("LAST15", mins[-15:])
        else:
            print(f"MINUTES date={date} freshness={date_state} count=0")
        result["minutes"] = minute_payload
        minute_ok = bool(mins) and date == today
    except Exception as exc:
        msg = f"MINUTE_ERROR {type(exc).__name__}: {exc}"
        result["errors"].append(msg)
        print(msg)

    if latest_ok and minute_ok:
        result["status"] = "OK"
    elif latest_ok or minute_ok:
        result["status"] = "PARTIAL"
    print(f"STATUS {result['status']}")
    return result


def fetch_index(now, name, secid):
    result = {"name": name, "status": "ERROR", "quote": None, "error": None}
    try:
        d = eastmoney(secid)
        if not d:
            raise RuntimeError("no data")
        qdt = market_dt(d.get("f86"))
        state, lag = freshness(now, qdt)
        result["quote"] = {
            "source": "Eastmoney",
            "latest": d.get("f43"),
            "change_percent": d.get("f170"),
            "open": d.get("f46"),
            "high": d.get("f44"),
            "low": d.get("f45"),
            "market_time_cst": fmt_dt(qdt),
            "lag_seconds": lag,
            "freshness": state,
        }
        result["status"] = "OK"
        lag_text = "n/a" if lag is None else f"{lag}s"
        print(
            f"{name} latest={d.get('f43')} pct={d.get('f170')}% "
            f"open={d.get('f46')} high={d.get('f44')} low={d.get('f45')} "
            f"time={fmt_dt(qdt)} lag={lag_text} freshness={state}"
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(f"{name} ERROR {result['error']}")
    return result


def main():
    now = datetime.now(CST)
    snapshot = {
        "schema_version": 1,
        "runner_time_cst": now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "runner_time_utc": now.astimezone(timezone.utc).isoformat(),
        "market_window": in_market_window(now),
        "stocks": {},
        "indices": {},
    }

    print("REALTIME_A_SHARE_V3")
    print(f"RUNNER_CST {snapshot['runner_time_cst']}")

    for stock in STOCKS:
        stock_result = fetch_stock(now, *stock)
        snapshot["stocks"][stock_result["code"]] = stock_result

    print("\n[MAJOR_INDICES]")
    for name, secid in INDICES:
        snapshot["indices"][name] = fetch_index(now, name, secid)

    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nSNAPSHOT_WRITTEN {SNAPSHOT_PATH} bytes={SNAPSHOT_PATH.stat().st_size}")
    print("END_REALTIME_A_SHARE_V3")


if __name__ == "__main__":
    main()
