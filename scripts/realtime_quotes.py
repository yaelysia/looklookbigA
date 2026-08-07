import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
SNAPSHOT_PATH = Path("snapshot.json")

# Detailed intraday tracking stays intentionally small.  The full A-share
# universe is fetched separately as a lightweight snapshot below.
DETAIL_CODES = ["002558", "600795"]

INDICES = [
    ("上证指数", "1.000001"),
    ("深证成指", "0.399001"),
    ("创业板指", "0.399006"),
]

QUOTE_FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f71,f86,f169,f170,f171"
ALL_A_FIELDS = "f2,f3,f4,f5,f6,f7,f8,f12,f13,f14,f15,f16,f17,f18,f20,f21,f124"
ALL_A_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


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


def infer_market(code: str, eastmoney_market=None) -> str:
    code = str(code).zfill(6)
    # Beijing Stock Exchange codes include legacy 4/8 prefixes and newer 920xxx.
    if code.startswith(("4", "8", "92")):
        return "BJ"
    if eastmoney_market == 1 or code.startswith(("5", "6", "9")):
        return "SH"
    return "SZ"


def infer_identifiers(code: str):
    code = str(code).zfill(6)
    market = infer_market(code)
    if market == "SH":
        return market, f"1.{code}", f"sh{code}"
    if market == "BJ":
        # Eastmoney uses market id 0 for BSE securities in its quote endpoints.
        return market, f"0.{code}", f"bj{code}"
    return market, f"0.{code}", f"sz{code}"


def eastmoney(secid: str):
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


def eastmoney_all_a_page(page: int, page_size: int = 500):
    params = {
        "pn": str(page),
        "pz": str(page_size),
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": ALL_A_FS,
        "fields": ALL_A_FIELDS,
        "_": str(int(time.time() * 1000)),
    }
    url = "https://push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
    payload = json.loads(http_get(url, timeout=15))
    return payload.get("data") or {}


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


def clean_num(value):
    if value in (None, "-", ""):
        return None
    return value


def print_minute_block(label, items):
    print(label)
    for item in items:
        print(
            f"  {item['time']} p={item['price']:.2f} "
            f"dvol={item['delta_volume']:.0f} damt={item['delta_amount'] / 1e6:.2f}m"
        )


def fetch_all_a_light(now: datetime):
    result = {
        "source": "Eastmoney clist",
        "status": "ERROR",
        "count": 0,
        "reported_total": None,
        "pages": 0,
        "market_time_cst": None,
        "lag_seconds": None,
        "freshness": "UNKNOWN",
        "stocks": {},
        "error": None,
    }
    try:
        page_size = 500
        page = 1
        reported_total = None
        max_quote_dt = None
        stocks = {}

        while True:
            data = eastmoney_all_a_page(page, page_size)
            if reported_total is None:
                reported_total = int(data.get("total") or 0)
            rows = data.get("diff") or []
            if isinstance(rows, dict):
                rows = list(rows.values())
            if not rows:
                break

            for row in rows:
                code = str(row.get("f12") or "").zfill(6)
                if len(code) != 6 or not code.isdigit():
                    continue
                em_market = row.get("f13")
                quote_dt = market_dt(row.get("f124"))
                if quote_dt and (max_quote_dt is None or quote_dt > max_quote_dt):
                    max_quote_dt = quote_dt
                stocks[code] = {
                    "code": code,
                    "name": row.get("f14"),
                    "market": infer_market(code, em_market),
                    "latest": clean_num(row.get("f2")),
                    "change_percent": clean_num(row.get("f3")),
                    "change": clean_num(row.get("f4")),
                    "volume_raw": clean_num(row.get("f5")),
                    "amount_raw": clean_num(row.get("f6")),
                    "amplitude_percent": clean_num(row.get("f7")),
                    "turnover_percent": clean_num(row.get("f8")),
                    "high": clean_num(row.get("f15")),
                    "low": clean_num(row.get("f16")),
                    "open": clean_num(row.get("f17")),
                    "previous_close": clean_num(row.get("f18")),
                    "total_market_cap": clean_num(row.get("f20")),
                    "float_market_cap": clean_num(row.get("f21")),
                    "market_time_cst": fmt_dt(quote_dt),
                }

            result["pages"] = page
            if reported_total and len(stocks) >= reported_total:
                break
            if len(rows) < page_size:
                break
            if reported_total and page >= math.ceil(reported_total / page_size):
                break
            page += 1
            if page > 20:
                raise RuntimeError("all-A pagination safety limit exceeded")

        state, lag = freshness(now, max_quote_dt)
        result.update(
            {
                "status": "OK" if stocks else "EMPTY",
                "count": len(stocks),
                "reported_total": reported_total,
                "market_time_cst": fmt_dt(max_quote_dt),
                "lag_seconds": lag,
                "freshness": state,
                "stocks": stocks,
            }
        )
        print(
            f"ALL_A status={result['status']} count={result['count']} "
            f"reported_total={reported_total} pages={result['pages']} "
            f"time={result['market_time_cst']} lag={lag}s freshness={state}"
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(f"ALL_A ERROR {result['error']}")
    return result


def fetch_stock(now, code):
    market, secid, tcode = infer_identifiers(code)
    print(f"\n[{code} detail market={market}]")
    result = {
        "code": code,
        "market": market,
        "name": code,
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
        result["name"] = d.get("f58") or code
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
            f"name={result['name']} latest={d.get('f43')} pct={d.get('f170')}% "
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
        "schema_version": 2,
        "runner_time_cst": now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "runner_time_utc": now.astimezone(timezone.utc).isoformat(),
        "market_window": in_market_window(now),
        "all_a_light": {},
        "stocks": {},
        "indices": {},
    }

    print("REALTIME_A_SHARE_V4")
    print(f"RUNNER_CST {snapshot['runner_time_cst']}")

    print("\n[ALL_A_LIGHT]")
    snapshot["all_a_light"] = fetch_all_a_light(now)

    print("\n[DETAIL_STOCKS]")
    for code in DETAIL_CODES:
        stock_result = fetch_stock(now, code)
        snapshot["stocks"][stock_result["code"]] = stock_result

    print("\n[MAJOR_INDICES]")
    for name, secid in INDICES:
        snapshot["indices"][name] = fetch_index(now, name, secid)

    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"\nSNAPSHOT_WRITTEN {SNAPSHOT_PATH} bytes={SNAPSHOT_PATH.stat().st_size}")
    print("END_REALTIME_A_SHARE_V4")


if __name__ == "__main__":
    main()
