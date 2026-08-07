import json
import os
from datetime import datetime
from pathlib import Path


CACHE_META = {}


def _history_root():
    return Path(os.environ.get("MARKET_HISTORY_DIR", ".market-data/history"))


def _validation_key(now):
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 15:
        phase = "preopen"
    elif hm < 15 * 60 + 5:
        phase = "intraday"
    else:
        phase = "closed"
    return f"{now.strftime('%Y-%m-%d')}:{phase}"


def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_bars(bars):
    out = {}
    for bar in bars or []:
        if not isinstance(bar, dict) or not bar.get("date"):
            continue
        out[str(bar["date"])] = bar
    return [out[k] for k in sorted(out)]


def _price_changed(a, b, tolerance=0.0015):
    try:
        a = float(a)
        b = float(b)
        if a == 0 and b == 0:
            return False
        base = max(abs(a), abs(b), 1e-9)
        return abs(a - b) / base > tolerance
    except (TypeError, ValueError):
        return False


def _adjustment_changed(cached_bars, recent_bars):
    cached = {x["date"]: x for x in cached_bars}
    overlap = [x for x in recent_bars if x.get("date") in cached]
    if not overlap:
        return True, 0

    mismatch = 0
    for fresh in overlap:
        old = cached[fresh["date"]]
        for key in ("open", "close", "high", "low"):
            if old.get(key) is None or fresh.get(key) is None:
                continue
            if _price_changed(old[key], fresh[key]):
                mismatch += 1
    return mismatch >= 2, len(overlap)


def _merge_bars(cached_bars, recent_bars, keep=120):
    merged = {x["date"]: x for x in cached_bars if x.get("date")}
    for bar in recent_bars:
        if bar.get("date"):
            merged[bar["date"]] = bar
    return [merged[k] for k in sorted(merged)][-keep:]


def _cache_path(code):
    return _history_root() / "daily_k" / f"{code}.json"


def _save_cache(code, source, bars, validation_key, validation_mode, now, errors=None):
    payload = {
        "schema_version": 1,
        "code": code,
        "adjustment": "qfq",
        "source": source,
        "validation_key": validation_key,
        "validation_mode": validation_mode,
        "updated_at_cst": now.strftime("%Y-%m-%d %H:%M:%S"),
        "bar_count": len(bars),
        "latest_bar_date": bars[-1]["date"] if bars else None,
        "errors": list(errors or []),
        "bars": bars,
    }
    _write_json(_cache_path(code), payload)
    return payload


def install_daily_k_cache(base, daily_k_context):
    original_fetch = daily_k_context.fetch_daily_bars

    def cached_fetch(base_obj, code, limit=90):
        now = datetime.now(base.CST)
        key = _validation_key(now)
        cached = _load_json(_cache_path(code))
        cached_bars = _normalize_bars((cached or {}).get("bars"))
        cached_source = (cached or {}).get("source") or "unknown"

        if len(cached_bars) >= 60 and (cached or {}).get("validation_key") == key:
            CACHE_META[code] = {
                "state": "HIT",
                "validation_key": key,
                "source": cached_source,
                "bar_count": len(cached_bars),
                "latest_bar_date": cached_bars[-1]["date"],
                "network_daily_k_requests": 0,
            }
            return f"History cache ({cached_source})", cached_bars[-max(limit, 60):], []

        if len(cached_bars) >= 60:
            refresh_errors = []
            try:
                recent_source, recent_bars, source_errors = original_fetch(base_obj, code, limit=8)
                refresh_errors.extend(source_errors or [])
                recent_bars = _normalize_bars(recent_bars)
                changed, overlap_count = _adjustment_changed(cached_bars, recent_bars)

                if changed:
                    full_source, full_bars, full_errors = original_fetch(base_obj, code, limit=max(120, limit))
                    refresh_errors.extend(full_errors or [])
                    full_bars = _normalize_bars(full_bars)[-120:]
                    saved = _save_cache(code, full_source, full_bars, key, "FULL_REFRESH_ADJUSTMENT_OR_GAP", now, refresh_errors)
                    CACHE_META[code] = {
                        "state": "FULL_REFRESH",
                        "validation_key": key,
                        "source": full_source,
                        "bar_count": len(full_bars),
                        "latest_bar_date": saved.get("latest_bar_date"),
                        "overlap_count": overlap_count,
                        "network_daily_k_requests": 2,
                    }
                    return full_source, full_bars[-max(limit, 60):], refresh_errors

                merged = _merge_bars(cached_bars, recent_bars, keep=120)
                saved = _save_cache(code, recent_source or cached_source, merged, key, "INCREMENTAL_VALIDATION", now, refresh_errors)
                CACHE_META[code] = {
                    "state": "INCREMENTAL_REFRESH",
                    "validation_key": key,
                    "source": recent_source or cached_source,
                    "bar_count": len(merged),
                    "latest_bar_date": saved.get("latest_bar_date"),
                    "overlap_count": overlap_count,
                    "network_daily_k_requests": 1,
                }
                return f"History cache + {recent_source}", merged[-max(limit, 60):], refresh_errors
            except Exception as exc:
                err = f"history validation: {type(exc).__name__}: {exc}"
                stale_errors = list((cached or {}).get("errors") or []) + [err]
                saved = _save_cache(code, cached_source, cached_bars, key, "STALE_CACHE_FALLBACK", now, stale_errors[-10:])
                CACHE_META[code] = {
                    "state": "STALE_FALLBACK",
                    "validation_key": key,
                    "source": cached_source,
                    "bar_count": len(cached_bars),
                    "latest_bar_date": saved.get("latest_bar_date"),
                    "network_daily_k_requests": 1,
                    "error": err,
                }
                return f"History stale cache ({cached_source})", cached_bars[-max(limit, 60):], [err]

        source, bars, errors = original_fetch(base_obj, code, limit=max(120, limit))
        bars = _normalize_bars(bars)[-120:]
        saved = _save_cache(code, source, bars, key, "BOOTSTRAP_FULL", now, errors)
        CACHE_META[code] = {
            "state": "BOOTSTRAP",
            "validation_key": key,
            "source": source,
            "bar_count": len(bars),
            "latest_bar_date": saved.get("latest_bar_date"),
            "network_daily_k_requests": 1,
        }
        return source, bars[-max(limit, 60):], errors

    daily_k_context.fetch_daily_bars = cached_fetch


def _compact_daily_context(ctx):
    if not isinstance(ctx, dict):
        return ctx
    return {k: v for k, v in ctx.items() if k != "bars_last_60"}


def _compact_minutes(minutes):
    if not isinstance(minutes, dict):
        return minutes
    return {k: v for k, v in minutes.items() if k not in ("first_10", "last_15")}


def _compact_snapshot(data):
    detail = {}
    for code, item in (data.get("detail_stocks") or {}).items():
        detail[code] = {
            "code": item.get("code"),
            "market": item.get("market"),
            "status": item.get("status"),
            "quote": item.get("quote"),
            "minutes": _compact_minutes(item.get("minutes")),
            "intraday": item.get("intraday"),
            "daily_context": _compact_daily_context(item.get("daily_context")),
            "errors": item.get("errors"),
        }
    return {
        "schema_version": data.get("schema_version"),
        "runner_time_cst": data.get("runner_time_cst"),
        "runner_time_utc": data.get("runner_time_utc"),
        "market_window": data.get("market_window"),
        "features": data.get("features"),
        "detail_stocks": detail,
        "groups": data.get("groups"),
        "indices": data.get("indices"),
    }


def _archive_snapshot(data):
    root = _history_root()
    stamp = str(data.get("runner_time_cst") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    date_part = stamp[:10]
    time_part = stamp[11:19].replace(":", "") if len(stamp) >= 19 else datetime.now().strftime("%H%M%S")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    rel = Path("snapshots") / date_part / f"{time_part}_run{run_id}_a{attempt}.json"
    _write_json(root / rel, _compact_snapshot(data))
    return str(rel).replace("\\", "/")


def _update_manifest(data, archive_rel):
    root = _history_root()
    daily_dir = root / "daily_k"
    codes = sorted(p.stem for p in daily_dir.glob("*.json")) if daily_dir.exists() else []
    manifest = {
        "schema_version": 1,
        "latest_snapshot": archive_rel,
        "latest_runner_time_cst": data.get("runner_time_cst"),
        "daily_k_codes": codes,
        "updated_at_cst": data.get("runner_time_cst"),
    }
    _write_json(root / "manifest.json", manifest)
    return manifest


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))

    for code, item in (data.get("detail_stocks") or {}).items():
        daily = item.get("daily_context")
        if isinstance(daily, dict):
            daily["cache"] = CACHE_META.get(code, {"state": "UNKNOWN"})

    data["schema_version"] = max(int(data.get("schema_version") or 0), 6)
    data.setdefault("features", {})["market_history"] = "v1"

    archive_rel = _archive_snapshot(data)
    manifest = _update_manifest(data, archive_rel)
    data["history"] = {
        "storage": "market-data branch",
        "archive_path": archive_rel,
        "manifest": manifest,
        "daily_k_cache": dict(CACHE_META),
    }

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    states = ",".join(f"{code}:{meta.get('state')}" for code, meta in sorted(CACHE_META.items()))
    requests = sum(int(meta.get("network_daily_k_requests") or 0) for meta in CACHE_META.values())
    print(f"HISTORY archive={archive_rel} daily_cache=[{states}] daily_k_network_requests={requests}", flush=True)
    print("SNAPSHOT_SCHEMA_UPGRADED schema_version=6 feature=market_history:v1", flush=True)
