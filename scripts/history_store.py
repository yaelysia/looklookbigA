import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


CACHE_META = {}
HIT_ELIGIBLE_VALIDATION_MODES = {
    "BOOTSTRAP_FULL",
    "INCREMENTAL_VALIDATION",
    "FULL_REFRESH_ADJUSTMENT_OR_GAP",
}


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
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


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
        cached_validation_mode = str((cached or {}).get("validation_mode") or "").upper()

        same_validation_key = (cached or {}).get("validation_key") == key
        reusable_validation = cached_validation_mode in HIT_ELIGIBLE_VALIDATION_MODES
        if len(cached_bars) >= 60 and same_validation_key and reusable_validation:
            CACHE_META[code] = {
                "state": "HIT",
                "validation_key": key,
                "validation_mode": cached_validation_mode,
                "source": cached_source,
                "bar_count": len(cached_bars),
                "latest_bar_date": cached_bars[-1]["date"],
                "network_daily_k_requests": 0,
            }
            return f"History cache ({cached_source})", cached_bars[-max(limit, 60):], []

        # A same-key cache is not automatically trustworthy. In particular,
        # STALE_CACHE_FALLBACK records the current validation key so that the
        # failure is auditable, but it must never gain HIT semantics on the next
        # run. Missing/unknown legacy validation modes are also revalidated.
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
                    mode = "FULL_REFRESH_ADJUSTMENT_OR_GAP"
                    saved = _save_cache(code, full_source, full_bars, key, mode, now, refresh_errors)
                    CACHE_META[code] = {
                        "state": "FULL_REFRESH",
                        "validation_key": key,
                        "validation_mode": mode,
                        "source": full_source,
                        "bar_count": len(full_bars),
                        "latest_bar_date": saved.get("latest_bar_date"),
                        "overlap_count": overlap_count,
                        "network_daily_k_requests": 2,
                    }
                    return full_source, full_bars[-max(limit, 60):], refresh_errors

                merged = _merge_bars(cached_bars, recent_bars, keep=120)
                mode = "INCREMENTAL_VALIDATION"
                saved = _save_cache(code, recent_source or cached_source, merged, key, mode, now, refresh_errors)
                CACHE_META[code] = {
                    "state": "INCREMENTAL_REFRESH",
                    "validation_key": key,
                    "validation_mode": mode,
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
                mode = "STALE_CACHE_FALLBACK"
                saved = _save_cache(code, cached_source, cached_bars, key, mode, now, stale_errors[-10:])
                CACHE_META[code] = {
                    "state": "STALE_FALLBACK",
                    "validation_key": key,
                    "validation_mode": mode,
                    "source": cached_source,
                    "bar_count": len(cached_bars),
                    "latest_bar_date": saved.get("latest_bar_date"),
                    "network_daily_k_requests": 1,
                    "error": err,
                }
                return f"History stale cache ({cached_source})", cached_bars[-max(limit, 60):], [err]

        source, bars, errors = original_fetch(base_obj, code, limit=max(120, limit))
        bars = _normalize_bars(bars)[-120:]
        mode = "BOOTSTRAP_FULL"
        saved = _save_cache(code, source, bars, key, mode, now, errors)
        CACHE_META[code] = {
            "state": "BOOTSTRAP",
            "validation_key": key,
            "validation_mode": mode,
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
            "minute_history": item.get("minute_history"),
            "relative_strength_windows": item.get("relative_strength_windows"),
            "intraday": item.get("intraday"),
            "daily_context": _compact_daily_context(item.get("daily_context")),
            "events": item.get("events"),
            "event_context": item.get("event_context"),
            "upcoming_events": item.get("upcoming_events"),
            "ownership_and_capital": item.get("ownership_and_capital"),
            "metadata": item.get("metadata"),
            "provenance": item.get("provenance"),
            "errors": item.get("errors"),
        }
    return {
        "schema_version": data.get("schema_version"),
        "runner_time_cst": data.get("runner_time_cst"),
        "runner_time_utc": data.get("runner_time_utc"),
        "observation": data.get("observation"),
        "market_calendar": data.get("market_calendar"),
        "minute_history": data.get("minute_history"),
        "market_window": data.get("market_window"),
        "features": data.get("features"),
        "detail_stocks": detail,
        "groups": data.get("groups"),
        "indices": data.get("indices"),
        "market_environment": data.get("market_environment"),
        "live_price_guard": data.get("live_price_guard"),
        "quote_resilience": data.get("quote_resilience"),
        "data_quality": data.get("data_quality"),
        "llm_data_summary": data.get("llm_data_summary"),
    }


def _archive_rel(data):
    stamp = str(data.get("runner_time_cst") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    date_part = stamp[:10]
    time_part = stamp[11:19].replace(":", "") if len(stamp) >= 19 else datetime.now().strftime("%H%M%S")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    return Path("snapshots") / date_part / f"{time_part}_run{run_id}_a{attempt}.json"


def _load_manifest():
    return _load_json(_history_root() / "manifest.json") or {}


def _build_manifest(data, archive_rel):
    root = _history_root()
    daily_dir = root / "daily_k"
    codes = sorted(p.stem for p in daily_dir.glob("*.json")) if daily_dir.exists() else []
    minute_dir = root / "minutes"
    minute_sessions = sorted(
        {
            path.parent.name
            for path in minute_dir.glob("*/*.json")
            if path.is_file()
        }
    ) if minute_dir.exists() else []
    return {
        "schema_version": 2,
        "latest_snapshot": str(archive_rel).replace("\\", "/"),
        "latest_runner_time_cst": data.get("runner_time_cst"),
        "daily_k_codes": codes,
        "minute_sessions": minute_sessions,
        "updated_at_cst": data.get("runner_time_cst"),
    }


def _safe_history_path(rel):
    root = _history_root().resolve()
    candidate = (root / str(rel)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("history snapshot path escapes history root") from exc
    return candidate


def load_previous_snapshot(data):
    history = data.get("history") or {}
    rel = history.get("previous_snapshot_path")
    if not rel:
        return None, None
    try:
        previous = _load_json(_safe_history_path(rel))
    except Exception:
        return None, None
    if isinstance(previous, dict):
        return previous, str(rel)
    return None, None


def finalize_snapshot(snapshot_path):
    """Prepare history context but do not archive the current run yet."""
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))

    for code, item in (data.get("detail_stocks") or {}).items():
        daily = item.get("daily_context")
        if isinstance(daily, dict):
            daily["cache"] = CACHE_META.get(code, {"state": "UNKNOWN"})

    data["schema_version"] = max(int(data.get("schema_version") or 0), 6)
    data.setdefault("features", {})["market_history"] = "v2"

    previous_manifest = _load_manifest()
    previous_path = previous_manifest.get("latest_snapshot")
    data["history"] = {
        "storage": "market-data branch / reusable history cache",
        "archive_path": None,
        "previous_snapshot_path": previous_path,
        "previous_manifest": previous_manifest or None,
        "manifest": None,
        "daily_k_cache": dict(CACHE_META),
    }

    _write_json(path, data)
    states = ",".join(f"{code}:{meta.get('state')}" for code, meta in sorted(CACHE_META.items()))
    requests = sum(int(meta.get("network_daily_k_requests") or 0) for meta in CACHE_META.values())
    print(
        f"HISTORY_PREPARED previous={previous_path} daily_cache=[{states}] "
        f"daily_k_network_requests={requests}",
        flush=True,
    )
    print("SNAPSHOT_SCHEMA_UPGRADED schema_version=6 feature=market_history:v2", flush=True)


def archive_final_snapshot(snapshot_path):
    """Archive the fully enriched snapshot, then advance the baseline pointer."""
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    rel = _archive_rel(data)
    rel_text = str(rel).replace("\\", "/")
    manifest = _build_manifest(data, rel)

    history = data.setdefault("history", {})
    history["archive_path"] = rel_text
    history["manifest"] = manifest
    _write_json(path, data)

    # Commit order matters: a baseline pointer must never advance before the
    # archive it points to exists.
    archive_path = _history_root() / rel
    compact = _compact_snapshot(data)
    existing = _load_json(archive_path) if archive_path.exists() else None
    if existing is not None and existing != compact:
        raise RuntimeError(f"immutable history archive already exists with different content: {rel_text}")
    if existing is None:
        _write_json(archive_path, compact)
    _write_json(_history_root() / "manifest.json", manifest)
    print(f"HISTORY_ARCHIVED archive={rel_text} schema={data.get('schema_version')}", flush=True)
