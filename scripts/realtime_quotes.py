import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

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
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "unknown"


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
                    "dvol": max(0.0, cum_vol - prev_vol),
                    "damount": max(0.0, cum_amount - prev_amount),
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
            f"dvol={item['dvol']:.0f} damt={item['damount'] / 1e6:.2f}m"
        )


def fetch_stock(now, code, secid, tcode, fallback_name):
    print(f"\n[{code} {fallback_name}]")

    latest_ok = False
    try:
        d = eastmoney(secid)
        if not d:
            raise RuntimeError("Eastmoney returned no data")
        qdt = market_dt(d.get("f86"))
        state, lag = freshness(now, qdt)
        lag_text = "n/a" if lag is None else f"{lag}s"
        print(
            "QUOTE "
            f"latest={d.get('f43')} pct={d.get('f170')}% "
            f"open={d.get('f46')} high={d.get('f44')} low={d.get('f45')} "
            f"avg={d.get('f71')} prev={d.get('f60')} "
            f"amount={float(d.get('f48') or 0) / 1e8:.2f}e8 "
            f"time={fmt_dt(qdt)} lag={lag_text} freshness={state}"
        )
        latest_ok = True
    except Exception as exc:
        print(f"QUOTE_ERROR {type(exc).__name__}: {exc}")

    minute_ok = False
    try:
        date, rows = tencent_minutes(tcode)
        mins = parse_minutes(rows)
        today = now.strftime("%Y%m%d")
        date_state = "LIVE" if date == today else "STALE"
        if mins:
            last_price = mins[-1]["price"]
            p5 = mins[-6]["price"] if len(mins) >= 6 else mins[0]["price"]
            p15 = mins[-16]["price"] if len(mins) >= 16 else mins[0]["price"]
            print(
                f"MINUTES date={date} freshness={date_state} count={len(mins)} "
                f"last={mins[-1]['time']}:{last_price:.2f} "
                f"trend5={fmt_pct(pct_change(last_price, p5))} "
                f"trend15={fmt_pct(pct_change(last_price, p15))}"
            )
            print_minute_block("FIRST10", mins[:10])
            print_minute_block("LAST15", mins[-15:])
        else:
            print(f"MINUTES date={date} freshness={date_state} count=0")
        minute_ok = bool(mins) and date == today
    except Exception as exc:
        print(f"MINUTE_ERROR {type(exc).__name__}: {exc}")

    if not latest_ok and not minute_ok:
        print("STATUS FAILED_BOTH_SOURCES")
    elif latest_ok and minute_ok:
        print("STATUS OK")
    else:
        print("STATUS PARTIAL")


def main():
    now = datetime.now(CST)
    print("REALTIME_A_SHARE_V2")
    print(f"RUNNER_CST {now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")

    for stock in STOCKS:
        fetch_stock(now, *stock)

    print("\n[MAJOR_INDICES]")
    for name, secid in INDICES:
        try:
            d = eastmoney(secid)
            if not d:
                raise RuntimeError("no data")
            qdt = market_dt(d.get("f86"))
            state, lag = freshness(now, qdt)
            lag_text = "n/a" if lag is None else f"{lag}s"
            print(
                f"{name} latest={d.get('f43')} pct={d.get('f170')}% "
                f"open={d.get('f46')} high={d.get('f44')} low={d.get('f45')} "
                f"time={fmt_dt(qdt)} lag={lag_text} freshness={state}"
            )
        except Exception as exc:
            print(f"{name} ERROR {type(exc).__name__}: {exc}")

    print("\nEND_REALTIME_A_SHARE_V2")


if __name__ == "__main__":
    main()
