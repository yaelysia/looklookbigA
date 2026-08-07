import json
import math
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import market_environment as env


EASTMONEY_FULL_LIST_URL = "https://82.push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_PAGE_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_UT = "bd1d9ddb04089700cf9c27f6f7426281"
SAMPLE_PAGE_SIZE = 100
SAMPLE_PAGE_TARGET = 8
MIN_SAMPLE_RECORDS = 300


def _query_url(endpoint, now, page, page_size, sort_field="f3"):
    params = {
        "pn": str(page),
        "pz": str(page_size),
        "po": "1",
        "np": "1",
        "ut": EASTMONEY_UT,
        "fltt": "2",
        "invt": "2",
        "fid": sort_field,
        "fs": env.MARKET_UNIVERSE_FS,
        "fields": env.MARKET_UNIVERSE_FIELDS,
        "_": str(int(now.timestamp() * 1000)),
    }
    return endpoint + "?" + urllib.parse.urlencode(params)


def _parse_page(text):
    payload = json.loads(text)
    data = payload.get("data") or {}
    records = env._normalize_diff(data.get("diff"))
    total = int(data.get("total") or len(records))
    return total, records


def _stats_for_subset(records):
    stats = env._summarize_market_records(records)
    return {
        "estimated": True,
        "sample_count": stats.get("count"),
        "sample_change_covered_count": stats.get("change_covered_count"),
        "sample_unavailable_change_count": stats.get("unavailable_change_count"),
        "sample_up_count": stats.get("up_count"),
        "sample_down_count": stats.get("down_count"),
        "sample_flat_count": stats.get("flat_count"),
        "up_ratio_percent": stats.get("up_ratio_percent"),
        "down_ratio_percent": stats.get("down_ratio_percent"),
        "breadth_score_percent": stats.get("breadth_score_percent"),
        "mean_change_percent": stats.get("mean_change_percent"),
        "median_change_percent": stats.get("median_change_percent"),
        "sample_amount_1e8": stats.get("amount_1e8"),
    }


def _estimated_overall(records, reported_total):
    stats = env._summarize_market_records(records)
    sample_count = int(stats.get("count") or 0)
    covered_sample = int(stats.get("change_covered_count") or 0)
    unavailable_sample = int(stats.get("unavailable_change_count") or 0)
    if sample_count <= 0 or covered_sample <= 0:
        raise RuntimeError("systematic market sample contains no usable change records")

    covered_est = int(round(reported_total * covered_sample / sample_count))
    covered_est = min(int(reported_total), max(0, covered_est))
    unavailable_est = max(0, int(reported_total) - covered_est)

    up_share_covered = (stats.get("up_count") or 0) / covered_sample
    down_share_covered = (stats.get("down_count") or 0) / covered_sample
    up_est = int(round(covered_est * up_share_covered))
    down_est = int(round(covered_est * down_share_covered))
    flat_est = max(0, covered_est - up_est - down_est)

    move_up_ratio = (stats.get("move_ge_3pct_count") or 0) / covered_sample
    move_down_ratio = (stats.get("move_le_minus_3pct_count") or 0) / covered_sample

    return {
        "estimated": True,
        "count": int(reported_total),
        "sample_count": sample_count,
        "change_covered_count": covered_est,
        "unavailable_change_count": unavailable_est,
        "count_semantics": "estimated_from_systematic_code_rank_sample",
        "up_count": up_est,
        "down_count": down_est,
        "flat_count": flat_est,
        "sample_change_covered_count": covered_sample,
        "sample_unavailable_change_count": unavailable_sample,
        "sample_up_count": stats.get("up_count"),
        "sample_down_count": stats.get("down_count"),
        "sample_flat_count": stats.get("flat_count"),
        "up_ratio_percent": stats.get("up_ratio_percent"),
        "down_ratio_percent": stats.get("down_ratio_percent"),
        "breadth_score_percent": stats.get("breadth_score_percent"),
        "up_share_of_universe_percent": env._round(up_est / reported_total * 100.0, 2) if reported_total else None,
        "down_share_of_universe_percent": env._round(down_est / reported_total * 100.0, 2) if reported_total else None,
        "unavailable_share_of_universe_percent": env._round(unavailable_est / reported_total * 100.0, 2) if reported_total else None,
        "mean_change_percent": stats.get("mean_change_percent"),
        "median_change_percent": stats.get("median_change_percent"),
        "amount_1e8": None,
        "sample_amount_1e8": stats.get("amount_1e8"),
        "amount_semantics": "sample_only_not_scaled",
        "move_ge_3pct_count": int(round(covered_est * move_up_ratio)),
        "move_le_minus_3pct_count": int(round(covered_est * move_down_ratio)),
        "limit_up_count_approx": None,
        "limit_down_count_approx": None,
        "broken_limit_up_count_approx": None,
    }


def _full_result(base, now):
    url = _query_url(
        EASTMONEY_FULL_LIST_URL,
        now,
        page=1,
        page_size=env.MARKET_UNIVERSE_PAGE_SIZE,
        sort_field="f3",
    )
    total, records = _parse_page(base.http_get(url, timeout=6, attempts=1))
    if not records:
        raise RuntimeError("Eastmoney full market universe returned no records")
    if total and len(records) < total:
        raise RuntimeError(f"Eastmoney full market universe was truncated: {len(records)}/{total}")

    result = {
        "status": "OK",
        "source": "Eastmoney clist full-universe",
        "snapshot_time_cst": now.strftime("%Y-%m-%d %H:%M:%S"),
        "freshness": "LIVE" if base.in_market_window(now) else "CURRENT_SESSION",
        "reported_total_count": total,
        "covered_count": len(records),
        "coverage_percent": 100.0,
        "estimated": False,
        "sampling": None,
        "overall": env._summarize_market_records(records),
        "boards": {},
        "exchanges": {},
        "limit_statistics": {
            "method": "approximate_by_board_price_limit_threshold",
            "available": True,
            "estimated": False,
            "note": "ST/board thresholds are handled; IPO/special trading rules can differ, so counts are diagnostic rather than exchange-certified.",
        },
    }
    for board in ("main_board", "chinext", "star_market", "bse"):
        subset = [x for x in records if env._board_for(x.get("f12")) == board]
        result["boards"][board] = env._summarize_market_records(subset)
    for exchange in ("SH", "SZ", "BJ"):
        subset = [x for x in records if env._exchange_for(x.get("f12")) == exchange]
        result["exchanges"][exchange] = env._summarize_market_records(subset)
    return result


def _sample_page(base, now, page):
    url = _query_url(
        EASTMONEY_PAGE_LIST_URL,
        now,
        page=page,
        page_size=SAMPLE_PAGE_SIZE,
        sort_field="f12",
    )
    return page, _parse_page(base.http_get(url, timeout=8, attempts=1))


def _sampled_result(base, now, primary_error):
    first_page, (reported_total, first_records) = _sample_page(base, now, 1)
    if not first_records or reported_total <= 0:
        raise RuntimeError("Eastmoney sampled market universe returned no records")

    page_count = max(1, int(math.ceil(reported_total / SAMPLE_PAGE_SIZE)))
    target = min(SAMPLE_PAGE_TARGET, page_count)
    if target == 1:
        pages = [1]
    else:
        pages = sorted(
            {
                1 + int(round(i * (page_count - 1) / (target - 1)))
                for i in range(target)
            }
        )

    page_records = {first_page: first_records}
    errors = []
    remaining = [page for page in pages if page != first_page]
    if remaining:
        with ThreadPoolExecutor(max_workers=min(4, len(remaining))) as pool:
            futures = {pool.submit(_sample_page, base, now, page): page for page in remaining}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    returned_page, (_, records) = future.result()
                    page_records[returned_page] = records
                except Exception as exc:
                    errors.append(f"page {page}: {type(exc).__name__}: {exc}")

    records_by_key = {}
    for page in sorted(page_records):
        for record in page_records[page]:
            key = f"{record.get('f13')}:{record.get('f12')}"
            records_by_key[key] = record
    records = list(records_by_key.values())
    if len(records) < min(MIN_SAMPLE_RECORDS, reported_total):
        raise RuntimeError(
            f"Eastmoney systematic sample too small: {len(records)}/{reported_total}; "
            f"page_errors={errors}"
        )

    overall = _estimated_overall(records, reported_total)
    boards = {}
    for board in ("main_board", "chinext", "star_market", "bse"):
        subset = [x for x in records if env._board_for(x.get("f12")) == board]
        boards[board] = _stats_for_subset(subset) if subset else {
            "estimated": True,
            "sample_count": 0,
            "status": "NO_SAMPLE",
        }
    exchanges = {}
    for exchange in ("SH", "SZ", "BJ"):
        subset = [x for x in records if env._exchange_for(x.get("f12")) == exchange]
        exchanges[exchange] = _stats_for_subset(subset) if subset else {
            "estimated": True,
            "sample_count": 0,
            "status": "NO_SAMPLE",
        }

    return {
        "status": "PARTIAL",
        "source": "Eastmoney clist systematic sample",
        "snapshot_time_cst": now.strftime("%Y-%m-%d %H:%M:%S"),
        "freshness": "LIVE" if base.in_market_window(now) else "CURRENT_SESSION",
        "reported_total_count": reported_total,
        "covered_count": len(records),
        "coverage_percent": env._round(len(records) / reported_total * 100.0, 2),
        "estimated": True,
        "partial_reason": "FULL_UNIVERSE_UNAVAILABLE_USING_SYSTEMATIC_SAMPLE",
        "primary_error": primary_error,
        "sampling": {
            "method": "systematic_even_pages_sorted_by_stock_code",
            "page_size": SAMPLE_PAGE_SIZE,
            "requested_pages": pages,
            "successful_pages": sorted(page_records),
            "page_errors": errors,
            "sample_count": len(records),
            "universe_count": reported_total,
            "sample_coverage_percent": env._round(len(records) / reported_total * 100.0, 2),
            "note": "Ratios and broad counts are estimates from a deterministic code-rank sample; total turnover and rare-event limit counts are intentionally not extrapolated.",
        },
        "overall": overall,
        "boards": boards,
        "exchanges": exchanges,
        "limit_statistics": {
            "method": "not_estimated_from_systematic_sample",
            "available": False,
            "estimated": False,
            "note": "Limit-up/down and broken-board counts are omitted in sampled mode because rare-event extrapolation would be unstable.",
        },
    }


def fetch_market_breadth(base, now):
    primary_error = None
    try:
        return _full_result(base, now)
    except Exception as exc:
        primary_error = f"{type(exc).__name__}: {exc}"

    try:
        return _sampled_result(base, now, primary_error)
    except Exception as exc:
        raise RuntimeError(
            "market breadth full-universe and sampled fallbacks both failed: "
            f"full={primary_error}; sample={type(exc).__name__}: {exc}"
        ) from exc
