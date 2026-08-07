import json
import math
import time
import urllib.parse

import realtime_quotes as base


def fetch_page_fast(page: int, page_size: int = 100, attempts: int = 1):
    params = {
        "pn": str(page),
        "pz": str(page_size),
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": base.ALL_A_FS,
        "fields": base.ALL_A_FIELDS,
        "_": f"{int(time.time() * 1000)}{page:03d}",
    }
    url = "https://push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
    payload = json.loads(base.http_get(url, timeout=4, attempts=attempts))
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
    deadline_seconds = 50
    page_size = 100

    try:
        first = fetch_page_fast(1, page_size, attempts=2)
        reported_total = int(first.get("total") or 0)
        total_pages = max(1, math.ceil(reported_total / page_size))
        stocks = {}
        max_quote_dt = base.normalize_all_a_rows(first.get("diff") or [], stocks, None)
        page_count = 1
        failed_pages = []

        # Sequential requests are deliberately used here. Eastmoney throttles a
        # burst of concurrent clist requests much more aggressively than a
        # short sequential scan.
        for page in range(2, total_pages + 1):
            if time.monotonic() - started > deadline_seconds:
                failed_pages.extend(
                    {"page": p, "error": "deadline_exceeded"}
                    for p in range(page, total_pages + 1)
                )
                break
            try:
                data = fetch_page_fast(page, page_size, attempts=1)
                max_quote_dt = base.normalize_all_a_rows(
                    data.get("diff") or [], stocks, max_quote_dt
                )
                page_count += 1
            except Exception as exc:
                failed_pages.append(
                    {"page": page, "error": f"{type(exc).__name__}: {exc}"}
                )
            time.sleep(0.03)

        # One quick second pass over failed pages while there is still budget.
        if failed_pages and time.monotonic() - started < deadline_seconds - 5:
            retry_candidates = [x["page"] for x in failed_pages if isinstance(x.get("page"), int)]
            retry_candidates = retry_candidates[:12]
            recovered = set()
            for page in retry_candidates:
                if time.monotonic() - started > deadline_seconds:
                    break
                try:
                    data = fetch_page_fast(page, page_size, attempts=1)
                    max_quote_dt = base.normalize_all_a_rows(
                        data.get("diff") or [], stocks, max_quote_dt
                    )
                    recovered.add(page)
                except Exception:
                    pass
            if recovered:
                failed_pages = [x for x in failed_pages if x.get("page") not in recovered]
                page_count += len(recovered)

        state, lag = base.freshness(now, max_quote_dt)
        complete = reported_total > 0 and len(stocks) >= reported_total and not failed_pages
        status = "OK" if complete else ("PARTIAL" if stocks else "EMPTY")
        result.update(
            {
                "status": status,
                "count": len(stocks),
                "reported_total": reported_total,
                "pages": page_count,
                "failed_pages": failed_pages,
                "market_time_cst": base.fmt_dt(max_quote_dt),
                "lag_seconds": lag,
                "freshness": state,
                "stocks": stocks,
            }
        )
        print(
            f"ALL_A_FAST status={status} count={len(stocks)} reported_total={reported_total} "
            f"pages={page_count}/{total_pages} failed={len(failed_pages)} "
            f"elapsed={time.monotonic() - started:.1f}s "
            f"time={result['market_time_cst']} lag={lag}s freshness={state}",
            flush=True,
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(f"ALL_A_FAST ERROR {result['error']}", flush=True)

    return result


base.fetch_all_a_light = fetch_all_a_light_fast

if __name__ == "__main__":
    base.main()
