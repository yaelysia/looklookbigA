import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import realtime_quotes_watchlist as base


def fetch_light_group_reliable(now, codes):
    results = {}
    if not codes:
        return results

    # Keep concurrency deliberately low: Eastmoney's per-symbol quote endpoint
    # intermittently returns 502 when a GitHub-hosted runner sends a larger burst.
    workers = min(2, len(codes))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(base.light_payload, now, code): code for code in codes}
        for future in as_completed(future_map):
            code = future_map[future]
            try:
                results[code] = future.result()
            except Exception as exc:
                results[code] = {
                    "status": "ERROR",
                    "quote": None,
                    "error": f"runner: {type(exc).__name__}: {exc}",
                }

    failed = [code for code in codes if results.get(code, {}).get("status") != "OK"]
    recovered = 0
    if failed:
        time.sleep(0.5)
        for code in failed:
            retry = base.light_payload(now, code)
            if retry.get("status") == "OK":
                results[code] = retry
                recovered += 1
            elif code not in results:
                results[code] = retry
            time.sleep(0.12)

    print(
        f"LIGHT_RETRY initial_failed={len(failed)} recovered={recovered} "
        f"remaining={len(failed) - recovered}",
        flush=True,
    )
    return dict(sorted(results.items()))


base.fetch_light_group = fetch_light_group_reliable

if __name__ == "__main__":
    base.main()
