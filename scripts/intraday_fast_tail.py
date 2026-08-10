import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import performance_fast_path


def install_fast_indices(base, quote_resilience):
    """Use one validated Tencent batch for FAST indices instead of six slow hedges.

    Both Eastmoney and Tencent are Trust-B providers. FULL keeps dual-source
    consensus; FAST explicitly trades cross-source consensus for bounded tail
    latency and marks that fact in resilience metadata.
    """
    if not performance_fast_path.is_fast():
        return

    def fast_indices(now):
        try:
            batch = quote_resilience._tencent_index_quotes(base, now)
            error = None
        except Exception as exc:
            batch = {}
            error = f"{type(exc).__name__}: {exc}"

        out = {}
        for name, _ in base.INDICES:
            quote = batch.get(name)
            if quote:
                quote = dict(quote)
                quote["resilience"] = {
                    "primary_source": quote_resilience.PRIMARY_SOURCE,
                    "fallback_source": quote_resilience.FALLBACK_SOURCE,
                    "selected_source": quote_resilience.FALLBACK_SOURCE,
                    "fallback_used": True,
                    "selection_reason": "INTRADAY_FAST_TENCENT_BATCH",
                    "consensus": {
                        "status": "SINGLE_SOURCE_FAST_PATH",
                        "price_gap": None,
                        "price_gap_percent": None,
                    },
                    "providers": {
                        quote_resilience.PRIMARY_SOURCE: {
                            "status": "SKIPPED_FAST_PATH",
                            "usable": None,
                            "latest": None,
                            "market_time_cst": None,
                            "lag_seconds": None,
                            "freshness": None,
                            "error": None,
                        },
                        quote_resilience.FALLBACK_SOURCE: quote_resilience._provider_state(
                            base, now, quote, None
                        ),
                    },
                }
                status = "OK" if quote_resilience._is_usable(base, now, quote) else "PARTIAL"
                out[name] = {"status": status, "quote": quote, "error": None}
            else:
                out[name] = {
                    "status": "ERROR",
                    "quote": None,
                    "error": error or "Tencent fast index batch returned no matching quote",
                }
        return out

    base.fetch_indices = fast_indices


def cache_only_market_breadth(base, now, indices):
    """Breadth never performs network I/O on the FAST critical path."""
    previous, rel = performance_fast_path._load_previous_snapshot()
    breadth = copy.deepcopy((((previous or {}).get("market_environment") or {}).get("breadth")) or {})
    collected = performance_fast_path._parse_cst_timestamp(breadth.get("collected_at_cst"), base.CST)
    age = max(0, int((now - collected).total_seconds())) if collected else None
    current_session = performance_fast_path._current_index_session(indices)
    same_session = bool(current_session and breadth.get("market_session_date") == current_session)

    if breadth and age is not None and age <= performance_fast_path.FAST_BREADTH_CACHE_MAX_AGE_SECONDS and same_session:
        breadth["source"] = f"Fast cache <- {breadth.get('source') or 'market breadth'}"
        breadth["fast_path"] = {
            "mode": performance_fast_path.MODE_FAST,
            "source": "HISTORY_CACHE_ONLY",
            "age_seconds": age,
            "max_age_seconds": performance_fast_path.FAST_BREADTH_CACHE_MAX_AGE_SECONDS,
            "source_snapshot": rel,
            "network_refresh": "DEFERRED_OUTSIDE_CRITICAL_PATH",
        }
        return breadth

    reason = "NO_CACHE"
    if breadth and not same_session:
        reason = "CACHE_SESSION_MISMATCH"
    elif breadth and age is None:
        reason = "CACHE_TIME_UNMEASURABLE"
    elif breadth and age > performance_fast_path.FAST_BREADTH_CACHE_MAX_AGE_SECONDS:
        reason = "CACHE_TOO_OLD"
    raise RuntimeError(f"fast breadth cache unavailable: {reason}")


def prefetch_company_events(config, company_events):
    started = time.monotonic()
    lookback = int(config.get("event_lookback_days") or company_events.DEFAULT_LOOKBACK_DAYS)
    if lookback not in company_events.ALLOWED_LOOKBACK_DAYS:
        raise ValueError("event lookback must be one of 7, 30, 90")
    now = datetime.now(company_events.CST)
    codes = list(config.get("detail_codes") or [])
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
                    "fetched_at": company_events._iso(now),
                    "latest": None,
                    "recent": [],
                    "upcoming": [],
                    "event_context": {
                        "count": 0,
                        "by_type": {},
                        "high_importance_event_ids": [],
                        "latest_high_importance_event_id": None,
                    },
                    "provider_health": {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"},
                    "error": f"{type(exc).__name__}: {exc}",
                }

    elapsed = time.monotonic() - started
    performance_fast_path.record_stage("company_events_prefetch", elapsed)
    return {
        "lookback_days": lookback,
        "started_at": company_events._iso(now),
        "results": results,
        "elapsed_ms": round(elapsed * 1000.0, 3),
    }


def apply_prefetched_company_events(snapshot_path, prefetched):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    results = prefetched.get("results") or {}
    lookback = int(prefetched.get("lookback_days") or 30)
    ok = degraded = failed = total_events = 0

    for code, item in (data.get("detail_stocks") or {}).items():
        events = results.get(code) or {
            "status": "ERROR",
            "source": "CNINFO",
            "source_tier": "OFFICIAL",
            "lookback_days": lookback,
            "latest": None,
            "recent": [],
            "upcoming": [],
            "event_context": {
                "count": 0,
                "by_type": {},
                "high_importance_event_ids": [],
                "latest_high_importance_event_id": None,
            },
            "error": "fast event prefetch result missing",
        }
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
        "detail_stock_count": len(data.get("detail_stocks") or {}),
        "ok_count": ok,
        "degraded_count": degraded,
        "error_count": failed,
        "recent_event_count": total_events,
        "fast_path": {
            "mode": performance_fast_path.MODE_FAST,
            "parallel": True,
            "overlapped_with_market_collection": True,
            "per_request_deadline_seconds": performance_fast_path.FAST_NETWORK_TIMEOUT_SECONDS,
            "prefetch_elapsed_ms": prefetched.get("elapsed_ms"),
            "pdf_fact_enrichment": "DEFERRED",
        },
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "COMPANY_EVENTS_FAST "
        f"status={data['company_events']['status']} lookback={lookback} "
        f"ok={ok} degraded={degraded} error={failed} recent_events={total_events} "
        f"prefetch_ms={prefetched.get('elapsed_ms')}",
        flush=True,
    )


def finalize_performance(snapshot_path):
    performance_fast_path.finalize_performance(snapshot_path)
    if not performance_fast_path.is_fast():
        return
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    perf = data.get("performance") or {}
    perf["fast_path_contract"] = {
        "critical": ["realtime_quote", "minute_series", "peer_quotes", "indices"],
        "detail_quotes": "Eastmoney/Tencent resilience retained with bounded request budgets",
        "indices": "single Tencent Trust-B batch; dual-source consensus deferred to FULL",
        "daily_k": "reuse current-day validated cache; otherwise explicit unverified fast reuse",
        "market_breadth": (
            "no network I/O; reuse same-session cache <= "
            f"{performance_fast_path.FAST_BREADTH_CACHE_MAX_AGE_SECONDS}s or mark unavailable"
        ),
        "company_events": (
            f"official CNINFO refresh in parallel with market collection; per-request budget "
            f"{performance_fast_path.FAST_NETWORK_TIMEOUT_SECONDS}s"
        ),
        "pdf_facts": "deferred to FULL",
        "history_persist": "outside decision-ready timing",
    }
    data["performance"] = perf
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
