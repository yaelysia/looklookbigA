import re
import time
import urllib.request
from datetime import datetime

import daily_k_context
import history_store
import intraday_metrics
import live_price_guard
import realtime_quotes_watchlist as base


# Install persistent daily-K caching first. Intraday metrics are built next,
# then the live-price guard sanitizes current-price inputs before daily-context
# support/resistance calculations consume them.
history_store.install_daily_k_cache(base, daily_k_context)
intraday_metrics.install(base)
live_price_guard.install(base)
daily_k_context.install(base)


def as_float(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_tencent_time(value):
    value = str(value or "").strip()
    try:
        if len(value) >= 14 and value[:14].isdigit():
            return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=base.CST)
    except ValueError:
        pass
    return None


def tencent_batch_quotes(now, codes):
    tcodes = []
    for code in codes:
        _, _, tcode = base.infer_identifiers(code)
        tcodes.append(tcode)

    urls = [
        "https://qt.gtimg.cn/q=" + ",".join(tcodes),
        "http://qt.gtimg.cn/q=" + ",".join(tcodes),
    ]
    last_error = None
    text = None
    for url in urls:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://gu.qq.com/",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                text = resp.read().decode("gbk", errors="replace")
            break
        except Exception as exc:
            last_error = exc

    if text is None:
        raise last_error or RuntimeError("Tencent batch quote returned no response")

    out = {}
    for match in re.finditer(r'v_([a-z]{2}\d{6})="([^"]*)"', text, flags=re.I):
        tcode = match.group(1).lower()
        fields = match.group(2).split("~")
        if len(fields) < 35:
            continue

        code = fields[2].zfill(6) if len(fields) > 2 else tcode[-6:]
        if code not in codes:
            continue

        latest = as_float(fields[3])
        previous_close = as_float(fields[4])
        open_price = as_float(fields[5])
        change = as_float(fields[31]) if len(fields) > 31 else None
        change_pct = as_float(fields[32]) if len(fields) > 32 else None
        high = as_float(fields[33]) if len(fields) > 33 else None
        low = as_float(fields[34]) if len(fields) > 34 else None
        volume_raw = as_float(fields[36]) if len(fields) > 36 else as_float(fields[6])
        amount_wan = as_float(fields[37]) if len(fields) > 37 else None

        if change is None and latest is not None and previous_close is not None:
            change = latest - previous_close
        if change_pct is None and latest is not None and previous_close:
            change_pct = (latest / previous_close - 1.0) * 100.0
        amplitude = None
        if high is not None and low is not None and previous_close:
            amplitude = (high - low) / previous_close * 100.0

        qdt = parse_tencent_time(fields[30] if len(fields) > 30 else None)
        freshness, lag = base.freshness(now, qdt)
        market, _, _ = base.infer_identifiers(code)
        amount_raw = amount_wan * 10000.0 if amount_wan is not None else None

        out[code] = {
            "status": "OK",
            "quote": {
                "code": code,
                "name": fields[1] if len(fields) > 1 else code,
                "market": market,
                "source": "Tencent batch",
                "latest": latest,
                "open": open_price,
                "high": high,
                "low": low,
                "average": None,
                "previous_close": previous_close,
                "change": change,
                "change_percent": change_pct,
                "amplitude_percent": amplitude,
                "volume_raw": volume_raw,
                "amount_raw": amount_raw,
                "amount_1e8": round(amount_wan / 10000.0, 4) if amount_wan is not None else None,
                "market_time_cst": base.fmt_dt(qdt),
                "lag_seconds": lag,
                "freshness": freshness,
            },
            "error": None,
        }

    return out


def fetch_light_group_reliable(now, codes):
    if not codes:
        return {}

    results = {}
    batch_error = None
    try:
        results.update(tencent_batch_quotes(now, codes))
    except Exception as exc:
        batch_error = f"{type(exc).__name__}: {exc}"

    missing = [code for code in codes if code not in results]
    recovered = 0
    for code in missing:
        retry = base.light_payload(now, code)
        results[code] = retry
        if retry.get("status") == "OK":
            recovered += 1
        time.sleep(0.12)

    ok = sum(1 for item in results.values() if item.get("status") == "OK")
    print(
        f"LIGHT_BATCH tencent={len(codes) - len(missing)}/{len(codes)} "
        f"fallback_recovered={recovered}/{len(missing)} final={ok}/{len(codes)} "
        f"batch_error={batch_error}",
        flush=True,
    )
    return dict(sorted(results.items()))


base.fetch_light_group = fetch_light_group_reliable

if __name__ == "__main__":
    base.main()
    intraday_metrics.finalize_snapshot(base.SNAPSHOT_PATH)
    daily_k_context.finalize_snapshot(base.SNAPSHOT_PATH)
    history_store.finalize_snapshot(base.SNAPSHOT_PATH)
    live_price_guard.finalize_snapshot(base.SNAPSHOT_PATH)
