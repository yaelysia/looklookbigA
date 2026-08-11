import json
import math
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import market_environment as env


EASTMONEY_FULL_LIST_URL = "https://82.push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_PAGE_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_UT = "bd1d9ddb04089700cf9c27f6f7426281"
SAMPLE_PAGE_SIZE = 100
SAMPLE_STRATA_TARGET = 8
SAMPLE_NEIGHBOR_ATTEMPTS = 3
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


def _session_anchor(indices):
    observations = []
    for name, item in (indices or {}).items():
        quote = (item or {}).get("quote") or {}
        market_time = str(quote.get("market_time_cst") or "")
        if len(market_time) < 10:
            continue
        observations.append(
            {
                "name": name,
                "market_time_cst": market_time,
                "market_session_date": market_time[:10],
                "freshness": quote.get("freshness") or "UNKNOWN",
            }
        )

    if not observations:
        return {
            "market_session_date": None,
            "freshness": "UNKNOWN",
            "freshness_basis": "NO_RELIABLE_SESSION_ANCHOR",
            "session_anchor": {
                "source": "indices",
                "members": [],
                "latest_market_time_cst": None,
            },
        }

    date_counts = Counter(x["market_session_date"] for x in observations)
    session_date = max(date_counts, key=lambda value: (date_counts[value], value))
    matching = [x for x in observations if x["market_session_date"] == session_date]
    freshness_counts = Counter(x["freshness"] for x in matching)
    if freshness_counts:
        top = freshness_counts.most_common()
        freshness = top[0][0] if len(top) == 1 or top[0][1] > top[1][1] else "UNKNOWN"
    else:
        freshness = "UNKNOWN"

    return {
        "market_session_date": session_date,
        "freshness": freshness,
        "freshness_basis": "INDEX_QUOTE_SESSION_ANCHOR",
        "session_anchor": {
            "source": "indices",
            "members": [x["name"] for x in matching],
            "latest_market_time_cst": max(x["market_time_cst"] for x in matching),
            "freshness_counts": dict(freshness_counts),
        },
    }


def _result_time_fields(now, indices):
    anchor = _session_anchor(indices)
    return {
        "collected_at_cst": now.strftime("%Y-%m-%d %H:%M:%S"),
        **anchor,
    }


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


def _estimate_stratum(records, population_count):
    stats = env._summarize_market_records(records)
    sample_count = int(stats.get("count") or 0)
    covered_sample = int(stats.get("change_covered_count") or 0)
    unavailable_sample = int(stats.get("unavailable_change_count") or 0)
    if sample_count <= 0 or covered_sample <= 0:
        raise RuntimeError("stratum sample contains no usable change records")

    covered_est = int(round(population_count * covered_sample / sample_count))
    covered_est = min(population_count, max(0, covered_est))
    unavailable_est = max(0, population_count - covered_est)
    up_est = int(round(covered_est * (stats.get("up_count") or 0) / covered_sample))
    down_est = int(round(covered_est * (stats.get("down_count") or 0) / covered_sample))
    flat_est = max(0, covered_est - up_est - down_est)
    move_up_est = int(round(covered_est * (stats.get("move_ge_3pct_count") or 0) / covered_sample))
    move_down_est = int(round(covered_est * (stats.get("move_le_minus_3pct_count") or 0) / covered_sample))

    return {
        "population_count": population_count,
        "sample_count": sample_count,
        "sample_change_covered_count": covered_sample,
        "sample_unavailable_change_count": unavailable_sample,
        "sample_up_count": stats.get("up_count"),
        "sample_down_count": stats.get("down_count"),
        "sample_flat_count": stats.get("flat_count"),
        "change_covered_count": covered_est,
        "unavailable_change_count": unavailable_est,
        "up_count": up_est,
        "down_count": down_est,
        "flat_count": flat_est,
        "move_ge_3pct_count": move_up_est,
        "move_le_minus_3pct_count": move_down_est,
    }


def _estimated_overall(stratum_results, reported_total):
    all_records = []
    estimates = []
    for result in stratum_results:
        records = result["records"]
        all_records.extend(records)
        estimates.append(_estimate_stratum(records, result["population_count"]))

    sample_stats = env._summarize_market_records(all_records)
    covered_est = sum(x["change_covered_count"] for x in estimates)
    unavailable_est = sum(x["unavailable_change_count"] for x in estimates)
    up_est = sum(x["up_count"] for x in estimates)
    down_est = sum(x["down_count"] for x in estimates)
    flat_est = sum(x["flat_count"] for x in estimates)
    move_up_est = sum(x["move_ge_3pct_count"] for x in estimates)
    move_down_est = sum(x["move_le_minus_3pct_count"] for x in estimates)

    return {
        "estimated": True,
        "count": int(reported_total),
        "sample_count": len(all_records),
        "change_covered_count": covered_est,
        "unavailable_change_count": unavailable_est,
        "count_semantics": "estimated_from_complete_stratified_code_rank_sample",
        "up_count": up_est,
        "down_count": down_est,
        "flat_count": flat_est,
        "sample_change_covered_count": sample_stats.get("change_covered_count"),
        "sample_unavailable_change_count": sample_stats.get("unavailable_change_count"),
        "sample_up_count": sample_stats.get("up_count"),
        "sample_down_count": sample_stats.get("down_count"),
        "sample_flat_count": sample_stats.get("flat_count"),
        "up_ratio_percent": env._round(up_est / covered_est * 100.0, 2) if covered_est else None,
        "down_ratio_percent": env._round(down_est / covered_est * 100.0, 2) if covered_est else None,
        "breadth_score_percent": env._round((up_est - down_est) / covered_est * 100.0, 2) if covered_est else None,
        "up_share_of_universe_percent": env._round(up_est / reported_total * 100.0, 2) if reported_total else None,
        "down_share_of_universe_percent": env._round(down_est / reported_total * 100.0, 2) if reported_total else None,
        "unavailable_share_of_universe_percent": env._round(unavailable_est / reported_total * 100.0, 2) if reported_total else None,
        "mean_change_percent": sample_stats.get("mean_change_percent"),
        "median_change_percent": sample_stats.get("median_change_percent"),
        "amount_1e8": None,
        "sample_amount_1e8": sample_stats.get("amount_1e8"),
        "amount_semantics": "sample_only_not_scaled",
        "move_ge_3pct_count": move_up_est,
        "move_le_minus_3pct_count": move_down_est,
        "limit_up_count_approx": None,
        "limit_down_count_approx": None,
        "broken_limit_up_count_approx": None,
    }


def _full_result(base, now, indices):
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
        **_result_time_fields(now, indices),
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
    return page, _parse_page(base.http_get(url, timeout=5, attempts=1))


def _discover_total(base, now):
    errors = []
    for page in (1, 2, 3):
        try:
            _, (reported_total, records) = _sample_page(base, now, page)
            if reported_total > 0 and records:
                return reported_total
        except Exception as exc:
            errors.append(f"page {page}: {type(exc).__name__}: {exc}")
    raise RuntimeError("unable to discover market universe size: " + "; ".join(errors))


def _build_strata(reported_total):
    page_count = max(1, int(math.ceil(reported_total / SAMPLE_PAGE_SIZE)))
    stratum_count = min(SAMPLE_STRATA_TARGET, page_count)
    strata = []
    for index in range(stratum_count):
        page_start = int(math.floor(index * page_count / stratum_count)) + 1
        page_end = int(math.floor((index + 1) * page_count / stratum_count))
        page_end = max(page_start, page_end)
        target_page = (page_start + page_end) // 2
        population_start = (page_start - 1) * SAMPLE_PAGE_SIZE
        population_end = min(reported_total, page_end * SAMPLE_PAGE_SIZE)
        strata.append(
            {
                "index": index,
                "page_start": page_start,
                "page_end": page_end,
                "target_page": target_page,
                "population_count": max(0, population_end - population_start),
            }
        )
    return strata


def _candidate_pages(stratum):
    target = stratum["target_page"]
    candidates = [target]
    distance = 1
    while len(candidates) < SAMPLE_NEIGHBOR_ATTEMPTS:
        added = False
        left = target - distance
        right = target + distance
        if left >= stratum["page_start"]:
            candidates.append(left)
            added = True
            if len(candidates) >= SAMPLE_NEIGHBOR_ATTEMPTS:
                break
        if right <= stratum["page_end"]:
            candidates.append(right)
            added = True
            if len(candidates) >= SAMPLE_NEIGHBOR_ATTEMPTS:
                break
        if not added:
            break
        distance += 1
    return candidates


def _fetch_stratum(base, now, stratum):
    attempted_pages = []
    errors = []
    for page in _candidate_pages(stratum):
        attempted_pages.append(page)
        try:
            _, (_, records) = _sample_page(base, now, page)
            if records:
                return {
                    **stratum,
                    "selected_page": page,
                    "attempted_pages": attempted_pages,
                    "sample_count": len(records),
                    "records": records,
                    "errors": errors,
                }
            errors.append(f"page {page}: empty")
        except Exception as exc:
            errors.append(f"page {page}: {type(exc).__name__}: {exc}")
    raise RuntimeError(
        f"stratum {stratum['index']} pages {stratum['page_start']}-{stratum['page_end']} failed: "
        + "; ".join(errors)
    )


def _sampled_result(base, now, indices, primary_error):
    reported_total = _discover_total(base, now)
    strata = _build_strata(reported_total)
    results = []
    failures = []

    with ThreadPoolExecutor(max_workers=min(4, len(strata))) as pool:
        futures = {pool.submit(_fetch_stratum, base, now, stratum): stratum for stratum in strata}
        for future in as_completed(futures):
            stratum = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                failures.append(
                    {
                        "index": stratum["index"],
                        "page_start": stratum["page_start"],
                        "page_end": stratum["page_end"],
                        "target_page": stratum["target_page"],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

    results.sort(key=lambda item: item["index"])
    if failures or len(results) != len(strata):
        raise RuntimeError(
            "incomplete stratified breadth coverage; no universe extrapolation allowed: "
            + json.dumps(
                {
                    "required_strata": len(strata),
                    "successful_strata": len(results),
                    "failed_strata": failures,
                },
                ensure_ascii=False,
            )
        )

    records_by_key = {}
    for result in results:
        for record in result["records"]:
            key = f"{record.get('f13')}:{record.get('f12')}"
            records_by_key[key] = record
    records = list(records_by_key.values())
    if len(records) < min(MIN_SAMPLE_RECORDS, reported_total):
        raise RuntimeError(f"complete stratified sample is still too small: {len(records)}/{reported_total}")

    overall = _estimated_overall(results, reported_total)
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

    sampling_strata = []
    for result in results:
        sampling_strata.append(
            {
                "index": result["index"],
                "page_start": result["page_start"],
                "page_end": result["page_end"],
                "target_page": result["target_page"],
                "selected_page": result["selected_page"],
                "attempted_pages": result["attempted_pages"],
                "population_count": result["population_count"],
                "sample_count": result["sample_count"],
                "fallback_used": result["selected_page"] != result["target_page"],
                "errors": result["errors"],
            }
        )

    return {
        "status": "PARTIAL",
        "source": "Eastmoney clist complete stratified sample",
        **_result_time_fields(now, indices),
        "reported_total_count": reported_total,
        "covered_count": len(records),
        "coverage_percent": env._round(len(records) / reported_total * 100.0, 2),
        "estimated": True,
        "partial_reason": "FULL_UNIVERSE_UNAVAILABLE_USING_COMPLETE_STRATIFIED_SAMPLE",
        "primary_error": primary_error,
        "sampling": {
            "method": "complete_stratified_code_rank_sample",
            "page_size": SAMPLE_PAGE_SIZE,
            "required_strata_count": len(strata),
            "successful_strata_count": len(results),
            "all_strata_covered": True,
            "neighbor_attempt_limit": SAMPLE_NEIGHBOR_ATTEMPTS,
            "sample_count": len(records),
            "universe_count": reported_total,
            "sample_coverage_percent": env._round(len(records) / reported_total * 100.0, 2),
            "strata": sampling_strata,
            "note": "Every rank stratum must yield one page; failed targets retry nearby pages inside the same stratum. If any stratum remains uncovered, no universe counts are extrapolated.",
        },
        "overall": overall,
        "boards": boards,
        "exchanges": exchanges,
        "limit_statistics": {
            "method": "not_estimated_from_stratified_sample",
            "available": False,
            "estimated": False,
            "note": "Limit-up/down and broken-board counts are omitted in sampled mode because rare-event extrapolation would be unstable.",
        },
    }


def fetch_market_breadth(base, now, indices=None):
    primary_error = None
    try:
        return _full_result(base, now, indices)
    except Exception as exc:
        primary_error = f"{type(exc).__name__}: {exc}"

    try:
        return _sampled_result(base, now, indices, primary_error)
    except Exception as exc:
        raise RuntimeError(
            "market breadth full-universe and stratified fallbacks both failed: "
            f"full={primary_error}; sample={type(exc).__name__}: {exc}"
        ) from exc
