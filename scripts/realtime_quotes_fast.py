import json
import time
import urllib.parse

import realtime_quotes as base

EASTMONEY_UT = "bd1d9ddb04089700cf9c27f6f7426281"


def fetch_page(page: int, page_size: int, attempts: int = 2):
    params = {
        "pn": str(page),
        "pz": str(page_size),
        "po": "1",
        "np": "1",
        "ut": EASTMONEY_UT,
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": base.ALL_A_FS,
        "fields": base.ALL_A_FIELDS,
        "_": f"{int(time.time() * 1000)}{page:03d}",
    }
    url = "https://push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
    payload = json.loads(base.http_get(url, timeout=8, attempts=attempts))
    return payload.get("data") or {}


def fetch_all_a_light_fast(now):
    result = {
        "source": "Eastmoney clist",
        "status": "ERROR",
        "count": 0,
        "reported_total": None,
        "pages": 0,
        "failed_pages": [],
        "market_time_cst": None,
        "lag_seconds": None,
        "freshness": "UNKNOWN",
        "stocks": {},
        "error": None,
    }
    started = time.monotonic()
    try:
        data = fetch_page(1, 10000, attempts=2)
        reported_total = int(data.get("total") or 0)
        rows = data.get("diff") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        stocks = {}
        max_quote_dt = base.normalize_all_a_rows(rows, stocks, None)
        state, lag = base.freshness(now, max_quote_dt)
        complete = reported_total > 0 and len(stocks) >= reported_total
        result.update(
            {
                "status": "OK" if complete else "PARTIAL",
                "count": len(stocks),
                "reported_total": reported_total,
                "pages": 1,
                "failed_pages": [],
                "market_time_cst": base.fmt_dt(max_quote_dt),
                "lag_seconds": lag,
                "freshness": state,
                "stocks": stocks,
            }
        )
        print(
            f"ALL_A_ONESHOT status={result['status']} count={len(stocks)} "
            f"reported_total={reported_total} requested_pz=10000 "
            f"elapsed={time.monotonic() - started:.1f}s freshness={state}",
            flush=True,
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(f"ALL_A_ONESHOT ERROR {result['error']}", flush=True)
    return result


base.fetch_all_a_light = fetch_all_a_light_fast

if __name__ == "__main__":
    base.main()
