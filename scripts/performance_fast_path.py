import copy
import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


MODE_AUTO = "AUTO"
MODE_FAST = "INTRADAY_FAST"
MODE_FULL = "FULL"
FAST_NETWORK_TIMEOUT_SECONDS = 3
FAST_BREADTH_TIMEOUT_SECONDS = 2
FAST_BREADTH_CACHE_MAX_AGE_SECONDS = 600
FAST_DECISION_TARGET_MS = 10_000
FAST_DECISION_HARD_LIMIT_MS = 15_000

_STAGE_LOCK = threading.Lock()
_STAGE_MS = {}
_RUNTIME_STARTED = time.monotonic()


def reset_telemetry():
    global _RUNTIME_STARTED
    with _STAGE_LOCK:
        _STAGE_MS.clear()
    _RUNTIME_STARTED = time.monotonic()


def record_stage(name, elapsed_seconds):
    with _STAGE_LOCK:
        _STAGE_MS[name] = round(float(elapsed_seconds) * 1000.0, 3)


def timed_call(name, fn, *args, **kwargs):
    started = time.monotonic()
    try:
        return fn(*args, **kwargs)
    finally:
        record_stage(name, time.monotonic() - started)


def resolve_mode(base, requested=None, now=None):
    requested = str(requested or os.environ.get("LOOKLOOK_EXECUTION_MODE") or MODE_AUTO).strip().upper()
    aliases = {
        "FAST": MODE_FAST,
        "INTRADAY": MODE_FAST,
        "INTRADAY_FAST": MODE_FAST,
        "FULL": MODE_FULL,
        "AUTO": MODE_AUTO,
        "": MODE_AUTO,
    }
    if requested not in aliases:
        raise ValueError("execution mode must be AUTO, INTRADAY_FAST/FAST, or FULL")
    normalized = aliases[requested]
    if normalized != MODE_AUTO:
        return normalized
    now = now or datetime.now(base.CST)
    return MODE_FAST if base.in_market_window(now) else MODE_FULL


def configure_mode(base, requested=None):
    mode = resolve_mode(base, requested=requested)
    os.environ["LOOKLOOK_EXECUTION_MODE"] = mode
    reset_telemetry()
    print(f"EXECUTION_MODE mode={mode}", flush=True)
    return mode


def current_mode():
    return str(os.environ.get("LOOKLOOK_EXECUTION_MODE") or MODE_FULL).upper()


def is_fast():
    return current_mode() == MODE_FAST


def _fast_http_wrapper(original):
    def fast_http(url, timeout=8, attempts=3):
        return original(
            url,
            timeout=min(float(timeout), FAST_NETWORK_TIMEOUT_SECONDS),
            attempts=1,
        )

    return fast_http


def install_network_deadlines(base, quote_resilience, company_events):
    if not is_fast():
        return
    if not getattr(base, "_fast_http_deadline_installed", False):
        base.http_get = _fast_http_wrapper(base.http_get)
        base._fast_http_deadline_installed = True

    if not getattr(quote_resilience, "_fast_tencent_deadline_installed", False):
        def fast_tencent_text(tcodes):
            joined = ",".join(tcodes)
            url = "https://qt.gtimg.cn/q=" + joined
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://gu.qq.com/",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            with urllib.request.urlopen(req, timeout=FAST_NETWORK_TIMEOUT_SECONDS) as resp:
                return resp.read().decode("gbk", errors="replace")

        quote_resilience._fetch_tencent_text = fast_tencent_text
        quote_resilience._fast_tencent_deadline_installed = True

    if not getattr(company_events, "_fast_request_deadline_installed", False):
        original_request_json = company_events._request_json

        def fast_request_json(url, method="GET", form=None, timeout=10, attempts=2):
            return original_request_json(
                url,
                method=method,
                form=form,
                timeout=min(float(timeout), FAST_NETWORK_TIMEOUT_SECONDS),
                attempts=1,
            )

        company_events._request_json = fast_request_json
        company_events._fast_request_deadline_installed = True


def install_concurrent_detail(base):
    if getattr(base, "_concurrent_detail_installed", False):
        return

    def concurrent_detail_payload(now, code):
        market, _, tcode = base.infer_identifiers(code)
        result = {
            "code": code,
            "market": market,
            "quote": None,
            "minutes": None,
            "status": "FAILED",
            "errors": [],
        }

        def fetch_quote():
            return base.quote_payload(now, code)

        def fetch_minutes():
            date, rows = base.tencent_minutes(tcode)
            mins = base.parse_minutes(rows)
            today = now.strftime("%Y%m%d")
            last_price = mins[-1]["price"] if mins else None
            p5 = mins[-6]["price"] if len(mins) >= 6 else (mins[0]["price"] if mins else None)
            p15 = mins[-16]["price"] if len(mins) >= 16 else (mins[0]["price"] if mins else None)
            return {
                "source": "Tencent",
                "date": date,
                "freshness": "LIVE" if date == today else "STALE",
                "count": len(mins),
                "last_time": mins[-1]["time"] if mins else None,
                "last_price": last_price,
                "trend_5m_percent": base.pct_change(last_price, p5),
                "trend_15m_percent": base.pct_change(last_price, p15),
                "first_10": mins[:10],
                "last_15": mins[-15:],
            }

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(fetch_quote): "quote",
                pool.submit(fetch_minutes): "minutes",
            }
            for future in as_completed(futures):
                kind = futures[future]
                try:
                    result[kind] = future.result()
                except Exception as exc:
                    result["errors"].append(f"{kind}: {type(exc).__name__}: {exc}")

        if result["quote"] and result["minutes"] and result["minutes"].get("freshness") == "LIVE":
            result["status"] = "OK"
        elif result["quote"] or result["minutes"]:
            result["status"] = "PARTIAL"
        return result

    base.detail_payload = concurrent_detail_payload
    base._concurrent_detail_installed = True


def install_fast_daily_cache(history_store, base, daily_k_context):
    if not is_fast():
        history_store.install_daily_k_cache(base, daily_k_context)
        return

    original_fetch = daily_k_context.fetch_daily_bars

    def cached_fetch(base_obj, code, limit=90):
        now = datetime.now(base.CST)
        cached = history_store._load_json(history_store._cache_path(code)) or {}
        cached_bars = history_store._normalize_bars(cached.get("bars"))
        cached_source = cached.get("source") or "unknown"
        validation_mode = str(cached.get("validation_mode") or "").upper()
        updated_at = str(cached.get("updated_at_cst") or "")
        validated_today = updated_at[:10] == now.strftime("%Y-%m-%d")
        hit_eligible = validation_mode in history_store.HIT_ELIGIBLE_VALIDATION_MODES

        if len(cached_bars) >= 60:
            if hit_eligible and validated_today:
                state = "HIT"
                fast_reuse_unverified = False
            else:
                state = "FAST_REUSE_UNVERIFIED"
                fast_reuse_unverified = True
            history_store.CACHE_META[code] = {
                "state": state,
                "validation_key": cached.get("validation_key"),
                "validation_mode": validation_mode or None,
                "source": cached_source,
                "bar_count": len(cached_bars),
                "latest_bar_date": cached_bars[-1]["date"],
                "network_daily_k_requests": 0,
                "fast_path": True,
                "fast_reuse_unverified": fast_reuse_unverified,
                "cache_updated_at_cst": cached.get("updated_at_cst"),
            }
            label = "History cache" if state == "HIT" else "History fast cache"
            return f"{label} ({cached_source})", cached_bars[-max(limit, 60):], []

        source, bars, errors = original_fetch(base_obj, code, limit=max(120, limit))
        bars = history_store._normalize_bars(bars)[-120:]
        key = history_store._validation_key(now)
        saved = history_store._save_cache(code, source, bars, key, "BOOTSTRAP_FULL", now, errors)
        history_store.CACHE_META[code] = {
            "state": "BOOTSTRAP",
            "validation_key": key,
            "validation_mode": "BOOTSTRAP_FULL",
            "source": source,
            "bar_count": len(bars),
            "latest_bar_date": saved.get("latest_bar_date"),
            "network_daily_k_requests": 1,
            "fast_path": True,
        }
        return source, bars[-max(limit, 60):], errors

    daily_k_context.fetch_daily_bars = cached_fetch


def install_fast_daily_metadata(data_metadata):
    if not is_fast() or getattr(data_metadata, "_fast_daily_metadata_installed", False):
        return
    original = data_metadata._daily_metadata

    def daily_metadata(context, fetched_at):
        value = original(context, fetched_at)
        cache = (context or {}).get("cache") or {} if isinstance(context, dict) else {}
        if cache.get("state") == "FAST_REUSE_UNVERIFIED":
            value["quality"] = "DEGRADED"
            flags = list(value.get("quality_flags") or [])
            if "FAST_PATH_CACHE_REUSE_UNVERIFIED" not in flags:
                flags.append("FAST_PATH_CACHE_REUSE_UNVERIFIED")
            value["quality_flags"] = flags
        return value

    data_metadata._daily_metadata = daily_metadata
    data_metadata._fast_daily_metadata_installed = True


def _history_root():
    return Path(os.environ.get("MARKET_HISTORY_DIR", ".market-data/history"))


def _load_previous_snapshot():
    root = _history_root()
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        rel = manifest.get("latest_snapshot")
        if not rel:
            return None, None
        candidate = (root / rel).resolve()
        candidate.relative_to(root.resolve())
        return json.loads(candidate.read_text(encoding="utf-8")), rel
    except Exception:
        return None, None


def _parse_cst_timestamp(value, tz):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _current_index_session(indices):
    dates = []
    for item in (indices or {}).values():
        market_time = str(((item or {}).get("quote") or {}).get("market_time_cst") or "")
        if len(market_time) >= 10:
            dates.append(market_time[:10])
    if not dates:
        return None
    return max(set(dates), key=lambda value: (dates.count(value), value))


def fast_market_breadth(base, now, indices, market_breadth_source):
    class FastBase:
        @staticmethod
        def http_get(url, timeout=6, attempts=1):
            return base.http_get(url, timeout=min(float(timeout), FAST_BREADTH_TIMEOUT_SECONDS), attempts=1)

    try:
        value = market_breadth_source._full_result(FastBase(), now, indices)
        value["fast_path"] = {
            "mode": MODE_FAST,
            "source": "LIVE_FULL_UNIVERSE",
            "network_deadline_seconds": FAST_BREADTH_TIMEOUT_SECONDS,
        }
        return value
    except Exception as live_exc:
        previous, rel = _load_previous_snapshot()
        breadth = copy.deepcopy((((previous or {}).get("market_environment") or {}).get("breadth")) or {})
        collected = _parse_cst_timestamp(breadth.get("collected_at_cst"), base.CST)
        age = max(0, int((now - collected).total_seconds())) if collected else None
        current_session = _current_index_session(indices)
        same_session = bool(current_session and breadth.get("market_session_date") == current_session)
        if breadth and age is not None and age <= FAST_BREADTH_CACHE_MAX_AGE_SECONDS and same_session:
            breadth["source"] = f"Fast cache <- {breadth.get('source') or 'market breadth'}"
            breadth["fast_path"] = {
                "mode": MODE_FAST,
                "source": "HISTORY_CACHE",
                "age_seconds": age,
                "max_age_seconds": FAST_BREADTH_CACHE_MAX_AGE_SECONDS,
                "source_snapshot": rel,
                "live_refresh_error": f"{type(live_exc).__name__}: {live_exc}",
            }
            return breadth
        raise RuntimeError(
            "fast breadth unavailable within deadline and no same-session cache: "
            f"{type(live_exc).__name__}: {live_exc}"
        ) from live_exc


def finalize_company_events_fast(snapshot_path, config, company_events):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    lookback = int(config.get("event_lookback_days") or os.environ.get("EVENT_LOOKBACK_DAYS") or company_events.DEFAULT_LOOKBACK_DAYS)
    if lookback not in company_events.ALLOWED_LOOKBACK_DAYS:
        raise ValueError("event lookback must be one of 7, 30, 90")
    now = company_events._parse_time(data.get("runner_time_utc")) or company_events._parse_time(data.get("runner_time_cst")) or datetime.now(company_events.CST)
    codes = list((data.get("detail_stocks") or {}).keys())
    results = {}

    workers = min(4, max(1, len(codes)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(company_events.fetch_events_for_code, code, lookback, now): code
            for code in codes
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                results[code] = future.result()
            except Exception as exc:
                results[code] = {
                    "status": "ERROR",
                    "source": "CNINFO",
                    "source_tier": "OFFICIAL",
                    "lookback_days": lookback,
                    "latest": None,
                    "recent": [],
                    "upcoming": [],
                    "event_context": {"count": 0, "by_type": {}, "high_importance_event_ids": [], "latest_high_importance_event_id": None},
                    "error": f"{type(exc).__name__}: {exc}",
                }

    ok = degraded = failed = total_events = 0
    for code, item in (data.get("detail_stocks") or {}).items():
        events = results.get(code) or {}
        item["events"] = events
        item["event_context"] = events.get("event_context")
        status = events.get("status")
        if status == "OK":
            ok += 1
        elif status in {"PARTIAL", "DEGRADED"}:
            degraded += 1
        else:
            failed += 1
        total_events += len(events.get("recent") or [])

    data["schema_version"] = max(int(data.get("schema_version") or 0), 12)
    data.setdefault("features", {})["company_events"] = "v1"
    data["company_events"] = {
        "status": "ERROR" if failed and not (ok or degraded) else ("PARTIAL" if failed or degraded else "OK"),
        "source": "CNINFO",
        "source_tier": "OFFICIAL",
        "lookback_days": lookback,
        "detail_stock_count": len(codes),
        "ok_count": ok,
        "degraded_count": degraded,
        "error_count": failed,
        "recent_event_count": total_events,
        "fast_path": {
            "mode": MODE_FAST,
            "parallel": True,
            "per_request_deadline_seconds": FAST_NETWORK_TIMEOUT_SECONDS,
            "pdf_fact_enrichment": "DEFERRED",
        },
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "COMPANY_EVENTS_FAST "
        f"status={data['company_events']['status']} lookback={lookback} "
        f"ok={ok} degraded={degraded} error={failed} recent_events={total_events}",
        flush=True,
    )


def install_parallel_main(base):
    if getattr(base, "_parallel_main_installed", False):
        return

    def fetch_detail_group(now, codes):
        if not codes:
            return {}
        out = {}
        workers = min(4, len(codes))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(base.detail_payload, now, code): code for code in codes}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    out[code] = future.result()
                except Exception as exc:
                    out[code] = {
                        "code": code,
                        "status": "FAILED",
                        "quote": None,
                        "minutes": None,
                        "errors": [f"detail: {type(exc).__name__}: {exc}"],
                    }
        return dict(sorted(out.items()))

    def parallel_main():
        started = time.monotonic()
        now = datetime.now(base.CST)
        cfg = base.load_config()
        mode = current_mode()
        print("REALTIME_A_SHARE_WATCHLIST_V2", flush=True)
        print(
            f"RUNNER_CST {now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} "
            f"mode={mode} detail={len(cfg['detail_codes'])} light={len(cfg['light_codes'])} "
            f"groups={len(cfg['groups'])} max_total={cfg['max_total_codes']} truncated={cfg['truncated']}",
            flush=True,
        )

        with ThreadPoolExecutor(max_workers=3) as pool:
            tasks = {
                "detail_stocks": pool.submit(timed_call, "detail_stocks", fetch_detail_group, now, cfg["detail_codes"]),
                "light_stocks": pool.submit(timed_call, "light_stocks", base.fetch_light_group, now, cfg["light_codes"]),
                "indices_and_breadth": pool.submit(timed_call, "indices_and_breadth", base.fetch_indices, now),
            }
            detail = tasks["detail_stocks"].result()
            light = tasks["light_stocks"].result()
            indices = tasks["indices_and_breadth"].result()

        for code, item in detail.items():
            q = item.get("quote") or {}
            m = item.get("minutes") or {}
            print(
                f"DETAIL {code} {q.get('name', code)} status={item.get('status')} "
                f"latest={q.get('latest')} pct={q.get('change_percent')}% high={q.get('high')} low={q.get('low')} "
                f"quote_time={q.get('market_time_cst')} quote_live={q.get('freshness')} "
                f"minute_last={m.get('last_time')}:{m.get('last_price')}",
                flush=True,
            )

        ok_light = sum(1 for x in light.values() if x.get("status") == "OK")
        print(f"LIGHT status={ok_light}/{len(light)} ok", flush=True)

        group_started = time.monotonic()
        groups = {}
        for group_id, group in cfg["groups"].items():
            summary = base.build_group_summary(group_id, group, detail, light)
            groups[group_id] = summary
            target = summary["target"]
            print(
                f"GROUP {group_id} status={summary['status']} "
                f"coverage={summary['covered_member_count']}/{summary['requested_member_count']} "
                f"mean={summary['mean_change_percent']}% median={summary['median_change_percent']}% "
                f"up/down/flat={summary['up_count']}/{summary['down_count']}/{summary['flat_count']} "
                f"target={target['code']}:{target['change_percent']}% vs_mean={summary['target_vs_peer_mean_percent']}%",
                flush=True,
            )
        record_stage("group_summary", time.monotonic() - group_started)

        snapshot = {
            "schema_version": 3,
            "runner_time_cst": now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "runner_time_utc": now.astimezone(base.timezone.utc).isoformat(),
            "market_window": base.in_market_window(now),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "execution_mode": mode,
            "config": cfg,
            "detail_stocks": detail,
            "light_stocks": light,
            "groups": groups,
            "indices": indices,
        }
        write_started = time.monotonic()
        base.SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        record_stage("base_snapshot_write", time.monotonic() - write_started)
        print(
            f"SNAPSHOT_WRITTEN {base.SNAPSHOT_PATH} bytes={base.SNAPSHOT_PATH.stat().st_size} elapsed={snapshot['elapsed_seconds']}s",
            flush=True,
        )

    base.main = parallel_main
    base._parallel_main_installed = True


def finalize_performance(snapshot_path):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    total_ms = round((time.monotonic() - _RUNTIME_STARTED) * 1000.0, 3)
    with _STAGE_LOCK:
        stages = dict(sorted(_STAGE_MS.items()))
    mode = current_mode()
    data["performance"] = {
        "mode": mode,
        "decision_snapshot_ready_ms": total_ms,
        "target_ms": FAST_DECISION_TARGET_MS if mode == MODE_FAST else None,
        "hard_limit_ms": FAST_DECISION_HARD_LIMIT_MS if mode == MODE_FAST else None,
        "within_target": total_ms <= FAST_DECISION_TARGET_MS if mode == MODE_FAST else None,
        "within_hard_limit": total_ms <= FAST_DECISION_HARD_LIMIT_MS if mode == MODE_FAST else None,
        "stages_ms": stages,
        "fast_path_contract": {
            "critical": ["realtime_quote", "minute_series", "peer_quotes", "indices"],
            "daily_k": "reuse current-day validated cache; otherwise explicit unverified fast reuse",
            "market_breadth": f"live full-universe deadline {FAST_BREADTH_TIMEOUT_SECONDS}s, then same-session cache <= {FAST_BREADTH_CACHE_MAX_AGE_SECONDS}s",
            "company_events": f"parallel official refresh with {FAST_NETWORK_TIMEOUT_SECONDS}s per-request deadline",
            "pdf_facts": "deferred in INTRADAY_FAST",
        } if mode == MODE_FAST else None,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"PERFORMANCE mode={mode} decision_ready_ms={total_ms} stages={json.dumps(stages, ensure_ascii=False, separators=(',', ':'))}",
        flush=True,
    )


def cli_resolve(base, requested):
    print(f"mode={resolve_mode(base, requested=requested)}")
