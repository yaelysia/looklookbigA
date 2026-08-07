import json
import urllib.parse

import market_environment as env


EASTMONEY_LIST_URL = "https://82.push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_UT = "bd1d9ddb04089700cf9c27f6f7426281"


def fetch_market_breadth(base, now):
    params = {
        "pn": "1",
        "pz": str(env.MARKET_UNIVERSE_PAGE_SIZE),
        "po": "1",
        "np": "1",
        "ut": EASTMONEY_UT,
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": env.MARKET_UNIVERSE_FS,
        "fields": env.MARKET_UNIVERSE_FIELDS,
        "_": str(int(now.timestamp() * 1000)),
    }
    url = EASTMONEY_LIST_URL + "?" + urllib.parse.urlencode(params)
    payload = json.loads(base.http_get(url, timeout=12, attempts=2))
    data = payload.get("data") or {}
    records = env._normalize_diff(data.get("diff"))
    reported_total = int(data.get("total") or len(records))
    if not records:
        raise RuntimeError("Eastmoney market universe returned no records")

    coverage = len(records) / reported_total * 100.0 if reported_total else 100.0
    complete = reported_total == 0 or len(records) >= reported_total
    result = {
        "status": "OK" if complete else "PARTIAL",
        "source": "Eastmoney clist (82.push2)",
        "snapshot_time_cst": now.strftime("%Y-%m-%d %H:%M:%S"),
        "freshness": "LIVE" if base.in_market_window(now) else "CURRENT_SESSION",
        "reported_total_count": reported_total,
        "covered_count": len(records),
        "coverage_percent": env._round(coverage, 2),
        "overall": None,
        "boards": None,
        "exchanges": None,
        "limit_statistics": {
            "method": "approximate_by_board_price_limit_threshold",
            "available": complete,
            "note": "ST/board thresholds are handled; IPO/special trading rules can differ, so counts are diagnostic rather than exchange-certified.",
        },
    }

    if not complete:
        # clist is sorted by f3. A truncated first page is therefore a biased
        # top-gainers sample and must never be treated as market breadth.
        result["partial_reason"] = "SERVER_PAGE_CAP_BIASES_SORTED_SAMPLE"
        result["sample_only"] = {
            "count": len(records),
            "note": "Not used for regime/breadth calculations.",
        }
        return result

    result["overall"] = env._summarize_market_records(records)
    result["boards"] = {}
    for board in ("main_board", "chinext", "star_market", "bse"):
        subset = [x for x in records if env._board_for(x.get("f12")) == board]
        result["boards"][board] = env._summarize_market_records(subset)

    result["exchanges"] = {}
    for exchange in ("SH", "SZ", "BJ"):
        subset = [x for x in records if env._exchange_for(x.get("f12")) == exchange]
        result["exchanges"][exchange] = env._summarize_market_records(subset)
    return result
