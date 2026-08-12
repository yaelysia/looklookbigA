import json
from datetime import date
from pathlib import Path

import company_events
import data_metadata
import ownership_capital_base as core
import ownership_capital_event_state as event_state


CAPITAL_EVENT_TYPES = ("PLEDGE", "CONVERTIBLE_BOND", "PREFERRED_SHARES", "REFINANCING")
SIGNAL_CODES = (
    "CONTROLLING_HOLDER_INCREASING",
    "CONTROLLING_HOLDER_DECREASING",
    "OWNERSHIP_CONCENTRATION_RISING",
    "INSTITUTIONAL_HOLDING_RISING",
    "SHAREHOLDER_COUNT_RISING",
    "BUYBACK_IN_PROGRESS",
    "MAJOR_UNLOCK_UPCOMING",
    "CAPITAL_STRUCTURE_STABLE",
)


def _as_of_date(snapshot):
    for key in ("runner_time_cst", "runner_time_utc"):
        value = core._date(snapshot.get(key))
        if value:
            return value
    return date.today().isoformat()


def _all_events(code, stock):
    by_id = {}
    try:
        cache = company_events._read_json(company_events._event_cache_path(code))
    except Exception:
        cache = None
    if isinstance(cache, dict) and cache.get("source") in (None, "CNINFO"):
        for event in cache.get("events") or []:
            if isinstance(event, dict) and event.get("event_id"):
                by_id[str(event["event_id"])] = dict(event)
    container = (stock or {}).get("events") or {}
    values = []
    latest = container.get("latest")
    if isinstance(latest, dict):
        values.append(latest)
    for key in ("recent", "upcoming"):
        if isinstance(container.get(key), list):
            values.extend(container[key])
    for event in values:
        if isinstance(event, dict) and event.get("event_id"):
            old = by_id.get(str(event["event_id"]), {})
            old.update(event)
            by_id[str(event["event_id"])] = old
    events = list(by_id.values())
    events.sort(key=lambda item: (item.get("published_at") or "", item.get("event_id") or ""), reverse=True)
    return events, cache


def _number(mapping, *names):
    for name in names:
        value = core._as_float((mapping or {}).get(name))
        if value is not None:
            return value
    return None


def _numbers(values):
    return [value for value in (core._as_float(item) for item in values or []) if value is not None]


def _capital_event(event):
    facts = event.get("facts") if isinstance(event.get("facts"), dict) else {}
    event_type = event.get("event_type")
    if event_type == "PLEDGE":
        unscoped = _numbers(facts.get("pledge_percentages") or facts.get("percentages"))
    else:
        unscoped = _numbers(
            facts.get("potential_dilution_percentages") or facts.get("percentages")
        )
    amounts = facts.get("capital_amounts") or facts.get("amounts") or []
    return {
        "event_id": event.get("event_id"),
        "event_type": event_type,
        "status": event.get("status") or "UNKNOWN",
        "published_at": event.get("published_at"),
        "effective_date": event.get("effective_date"),
        "title": event.get("title"),
        "importance": event.get("importance"),
        "controller_pledge_ratio_percent": _number(
            facts, "controller_pledge_ratio_percent", "controlling_holder_pledge_ratio_percent"
        ),
        "overall_pledge_ratio_percent": _number(
            facts, "overall_pledge_ratio_percent", "total_pledge_ratio_percent"
        ),
        "potential_dilution_percent": _number(
            facts, "potential_dilution_percent", "dilution_ratio_percent"
        ),
        "provider_unscoped_percentages": unscoped,
        "capital_amounts": amounts if isinstance(amounts, list) else [],
        "quality_flags": (
            ["PERCENTAGE_SCOPE_UNAVAILABLE"]
            if unscoped
            and _number(
                facts,
                "controller_pledge_ratio_percent",
                "controlling_holder_pledge_ratio_percent",
                "overall_pledge_ratio_percent",
                "total_pledge_ratio_percent",
                "potential_dilution_percent",
                "dilution_ratio_percent",
            )
            is None
            else []
        ),
        "provenance": {
            "provider": "CNINFO",
            "source_tier": event.get("source_tier") or "OFFICIAL",
            "source_document_id": event.get("source_document_id"),
            "source_url": event.get("source_url"),
            "fact_extraction_scope": facts.get("extraction_scope"),
        },
    }


def normalize_capital_tools(events, coverage, as_of, fetched_at):
    history = [_capital_event(event) for event in events if event.get("event_type") in CAPITAL_EVENT_TYPES]
    buckets = {
        event_type.lower(): [item for item in history if item.get("event_type") == event_type]
        for event_type in CAPITAL_EVENT_TYPES
    }
    latest_pledge = buckets["pledge"][0] if buckets["pledge"] else None
    comparable_pledges = [
        item
        for item in buckets["pledge"]
        if item.get("overall_pledge_ratio_percent") is not None
    ]
    pledge_ratio_change = None
    if len(comparable_pledges) >= 2:
        pledge_ratio_change = core._round(
            comparable_pledges[0]["overall_pledge_ratio_percent"]
            - comparable_pledges[1]["overall_pledge_ratio_percent"],
            4,
        )
    flags = []
    if not coverage.get("sufficient_for_persistent_state"):
        flags.append("EVENT_HISTORY_COVERAGE_INSUFFICIENT_FOR_PERSISTENT_STATE")
    if any(item.get("quality_flags") for item in history):
        flags.append("SOME_CAPITAL_FACTS_HAVE_UNSCOPED_PERCENTAGES")
    status = "OK" if coverage.get("sufficient_for_persistent_state") and not flags else "PARTIAL"
    return {
        "status": status,
        "as_of_date": as_of,
        "controller_pledge_ratio_percent": (
            latest_pledge.get("controller_pledge_ratio_percent") if latest_pledge else None
        ),
        "overall_pledge_ratio_percent": (
            latest_pledge.get("overall_pledge_ratio_percent") if latest_pledge else None
        ),
        "pledge_ratio_change_pp": pledge_ratio_change,
        "pledges": buckets["pledge"],
        "convertible_bonds": buckets["convertible_bond"],
        "preferred_shares": buckets["preferred_shares"],
        "refinancing": buckets["refinancing"],
        "metadata": {
            "freshness": "OFFICIAL_EVENT_DERIVED_STATE",
            "realtime": False,
            "quality": "PASS" if status == "OK" else "PARTIAL",
            "quality_flags": flags,
            "coverage": coverage,
            "ratio_policy": "only explicitly scoped pledge/dilution ratios are promoted; title percentages remain unscoped",
            "sentiment_policy": "STRUCTURAL_FACTS_ONLY; NO_BULLISH_BEARISH_INFERENCE",
        },
        "provenance": {
            "provider": "CNINFO",
            "source_tier": "OFFICIAL",
            "fetched_at": fetched_at,
            "event_types": list(CAPITAL_EVENT_TYPES),
            "source_contract": "company_events normalized official disclosure events and enriched facts",
        },
    }


def _deferred_capital_tools(fetched_at):
    return {
        "status": "DEFERRED",
        "as_of_date": None,
        "controller_pledge_ratio_percent": None,
        "overall_pledge_ratio_percent": None,
        "pledge_ratio_change_pp": None,
        "pledges": [],
        "convertible_bonds": [],
        "preferred_shares": [],
        "refinancing": [],
        "metadata": {
            "freshness": "NOT_DERIVED_IN_INTRADAY_FAST",
            "realtime": False,
            "quality": "PARTIAL",
            "quality_flags": ["FULL_ONLY_PLEDGES_AND_CAPITAL_TOOLS"],
        },
        "provenance": {
            "provider": "CNINFO",
            "source_tier": "OFFICIAL",
            "fetched_at": fetched_at,
            "event_types": list(CAPITAL_EVENT_TYPES),
        },
    }


def _controller_holding_changes(context):
    controllers = context.get("controllers") or {}
    declared = (controllers.get("controlling_shareholder") or {}).get("holders") or []
    names = {item.get("name") for item in declared if item.get("name")}
    latest = (((context.get("top_holders") or {}).get("top_shareholders") or {}).get("latest") or {})
    changes = []
    for holder in latest.get("holders") or []:
        if holder.get("name") not in names:
            continue
        changes.append(
            {
                "name": holder.get("name"),
                "report_date": latest.get("report_date"),
                "change_shares": core._as_float(holder.get("change_shares")),
                "change_ratio_percent": core._as_float(holder.get("change_ratio_percent")),
                "hold_ratio_change_percent": core._as_float(holder.get("hold_ratio_change_percent")),
                "change_state": holder.get("change_state"),
                "evidence_path": "top_holders.top_shareholders.latest.holders",
            }
        )
    controllers["recent_holding_changes"] = changes


def _valuation_share_semantics(stock, context, fetched_at):
    quote = stock.get("quote") or {}
    share = context.get("share_structure") or {}
    values = share.get("values") or {}
    total_cap = core._as_float(quote.get("total_market_cap"))
    float_cap = core._as_float(quote.get("float_market_cap"))
    flags = []
    if total_cap is None:
        flags.append("TOTAL_MARKET_CAP_UNAVAILABLE")
    if float_cap is None:
        flags.append("FLOAT_MARKET_CAP_UNAVAILABLE")
    if values.get("total_shares") is None:
        flags.append("TOTAL_SHARE_DENOMINATOR_UNAVAILABLE")
    if values.get("float_shares") is None:
        flags.append("FLOAT_SHARE_DENOMINATOR_UNAVAILABLE")
    status = "OK" if not flags else "PARTIAL" if total_cap is not None or float_cap is not None or values else "UNAVAILABLE"
    return {
        "status": status,
        "as_of_date": share.get("as_of_date"),
        "total_market_cap": {"value_yuan": total_cap, "scope": "TOTAL_SHARES", "provider_field": "f20"},
        "float_market_cap": {"value_yuan": float_cap, "scope": "PROVIDER_FLOAT_MARKET_CAP", "provider_field": "f21"},
        "share_denominators": {
            "total_shares": core._as_float(values.get("total_shares")),
            "float_shares": core._as_float(values.get("float_shares")),
            "float_scope": values.get("float_scope"),
            "share_structure_as_of_date": share.get("as_of_date"),
        },
        "metadata": {
            "freshness": "MIXED_REALTIME_MARKET_CAP_AND_DISCLOSED_SHARE_DENOMINATORS",
            "realtime": False,
            "quality": "PASS" if status == "OK" else "PARTIAL" if status == "PARTIAL" else "FAILED",
            "quality_flags": flags,
            "semantic_policy": "market-cap fields are provider observations; share denominators are dated disclosures; no valuation inference",
        },
        "provenance": {
            "provider": "DERIVED",
            "source_tier": "INHERITED",
            "fetched_at": fetched_at,
            "derived_from": ["quote.total_market_cap", "quote.float_market_cap", "ownership_and_capital.share_structure"],
        },
    }


def _upcoming_events(stock, context, fetched_at):
    unlocks = context.get("unlocks") or {}
    events = []
    for item in unlocks.get("upcoming") or []:
        events.append(
            {
                "event_id": item.get("event_id"),
                "event_type": "UNLOCK",
                "effective_date": item.get("unlock_date"),
                "days_until": item.get("days_until"),
                "title": item.get("title"),
                "importance": item.get("importance"),
                "unlock_shares": item.get("unlock_shares"),
                "unlock_ratio_total_percent": item.get("unlock_ratio_total_percent"),
                "unlock_ratio_float_percent": item.get("unlock_ratio_float_percent"),
                "source_path": "ownership_and_capital.unlocks.upcoming",
            }
        )
    value = {
        "status": "OK" if unlocks.get("status") == "OK" else "DEFERRED" if unlocks.get("status") == "DEFERRED" else "PARTIAL",
        "as_of_date": unlocks.get("as_of_date"),
        "events": events,
        "metadata": {
            "freshness": (unlocks.get("metadata") or {}).get("freshness") or "UNKNOWN",
            "realtime": False,
            "quality": (unlocks.get("metadata") or {}).get("quality") or "PARTIAL",
            "quality_flags": list((unlocks.get("metadata") or {}).get("quality_flags") or []),
            "reuse_policy": "stable event_id references shared from ownership unlock state",
        },
        "provenance": {
            "provider": "CNINFO",
            "source_tier": "OFFICIAL",
            "fetched_at": fetched_at,
            "derived_from": "ownership_and_capital.unlocks.upcoming",
        },
    }
    stock["upcoming_events"] = value


def _assessment(state, evidence, reason=None):
    value = {"state": state, "evidence": evidence}
    if reason:
        value["reason"] = reason
    return value


def _capital_structure_stable(share):
    history = share.get("history") or []
    if len(history) < 2:
        return _assessment("UNKNOWN", [], "TWO_DATED_SHARE_STRUCTURE_PERIODS_REQUIRED")
    latest = history[0]
    change = latest.get("change_from_previous") or {}
    fields = ("total_shares", "float_shares", "restricted_shares")
    comparable = all((change.get(field) or {}).get("comparable") for field in fields)
    if not comparable:
        return _assessment("UNKNOWN", [change], "REQUIRED_SHARE_FIELDS_NOT_COMPARABLE")
    stable = all((change.get(field) or {}).get("delta") == 0 for field in fields)
    return _assessment(
        "CONFIRMED" if stable else "NOT_CONFIRMED",
        [{"latest_as_of_date": latest.get("as_of_date"), "changes": {field: change[field] for field in fields}}],
    )


def _structural_signals(context):
    controllers = context.get("controllers") or {}
    holder_changes = controllers.get("recent_holding_changes") or []
    comparable_holder_changes = [item for item in holder_changes if item.get("change_shares") is not None]
    if comparable_holder_changes:
        increasing = [item for item in comparable_holder_changes if item["change_shares"] > 0]
        decreasing = [item for item in comparable_holder_changes if item["change_shares"] < 0]
        inc = _assessment("CONFIRMED" if increasing else "NOT_CONFIRMED", increasing or comparable_holder_changes)
        dec = _assessment("CONFIRMED" if decreasing else "NOT_CONFIRMED", decreasing or comparable_holder_changes)
    else:
        inc = dec = _assessment("UNKNOWN", [], "NO_PROVIDER_DECLARED_CONTROLLING_HOLDER_CHANGE")

    concentration = ((context.get("top_holders") or {}).get("concentration_trend") or {})
    institution = ((context.get("institutional_holdings") or {}).get("trend") or {})
    shareholder = context.get("shareholder_count") or {}
    plans = context.get("buyback_and_holder_plans") or {}
    active_buybacks = ((plans.get("buybacks") or {}).get("confirmed_active") or [])
    unlocks = context.get("unlocks") or {}
    major_unlocks = [
        item
        for item in unlocks.get("upcoming") or []
        if (item.get("days_until") is not None and item["days_until"] <= 90)
        and (
            str(item.get("importance") or "").upper() == "HIGH"
            or (core._as_float(item.get("unlock_ratio_total_percent")) or 0) >= 5.0
        )
    ]
    concentration_state = concentration.get("state")
    institution_state = institution.get("state")
    shareholder_state = shareholder.get("trend")
    concentration_known = concentration_state in {"RISING", "FALLING", "STABLE"}
    institution_known = institution_state in {"RISING", "FALLING", "STABLE"}
    shareholder_known = shareholder_state in {
        "SHAREHOLDER_COUNT_RISING",
        "SHAREHOLDER_COUNT_FALLING",
        "STABLE",
        "VOLATILE",
    }
    plans_known = plans.get("status") in {"OK", "PARTIAL"}
    unlocks_known = unlocks.get("status") in {"OK", "PARTIAL"}

    assessments = {
        "CONTROLLING_HOLDER_INCREASING": inc,
        "CONTROLLING_HOLDER_DECREASING": dec,
        "OWNERSHIP_CONCENTRATION_RISING": _assessment(
            "UNKNOWN" if not concentration_known else "CONFIRMED" if concentration_state == "RISING" else "NOT_CONFIRMED",
            [concentration] if concentration else [],
        ),
        "INSTITUTIONAL_HOLDING_RISING": _assessment(
            "UNKNOWN" if not institution_known else "CONFIRMED" if institution_state == "RISING" else "NOT_CONFIRMED",
            [institution] if institution else [],
        ),
        "SHAREHOLDER_COUNT_RISING": _assessment(
            "UNKNOWN" if not shareholder_known else "CONFIRMED" if shareholder_state == "SHAREHOLDER_COUNT_RISING" else "NOT_CONFIRMED",
            [
                {
                    "trend": shareholder.get("trend"),
                    "as_of_date": shareholder.get("as_of_date"),
                    "window_trends": shareholder.get("window_trends"),
                }
            ],
        ),
        "BUYBACK_IN_PROGRESS": _assessment(
            "UNKNOWN" if not plans_known else "CONFIRMED" if active_buybacks else "NOT_CONFIRMED",
            active_buybacks,
        ),
        "MAJOR_UNLOCK_UPCOMING": _assessment(
            "UNKNOWN" if not unlocks_known else "CONFIRMED" if major_unlocks else "NOT_CONFIRMED",
            major_unlocks,
        ),
        "CAPITAL_STRUCTURE_STABLE": _capital_structure_stable(context.get("share_structure") or {}),
    }
    signals = [
        {"signal": code, "evidence": assessments[code]["evidence"]}
        for code in SIGNAL_CODES
        if assessments[code]["state"] == "CONFIRMED"
    ]
    dates = [
        (context.get(name) or {}).get("as_of_date")
        for name in (
            "share_structure",
            "controllers",
            "top_holders",
            "institutional_holdings",
            "shareholder_count",
            "buyback_and_holder_plans",
            "unlocks",
        )
    ]
    return {
        "status": "OK" if all(item["state"] != "UNKNOWN" for item in assessments.values()) else "PARTIAL",
        "as_of_date": max((value for value in dates if value), default=None),
        "signals": signals,
        "assessments": assessments,
        "metadata": {
            "freshness": "DERIVED_FROM_DISCLOSED_OWNERSHIP_FACTS",
            "realtime": False,
            "quality": "PASS" if all(item["state"] != "UNKNOWN" for item in assessments.values()) else "PARTIAL",
            "quality_flags": [code for code, item in assessments.items() if item["state"] == "UNKNOWN"],
            "semantic_policy": "STRUCTURAL_FACTS_ONLY; NO_BULLISH_BEARISH_OR_TRADE_INFERENCE",
        },
        "provenance": {
            "provider": "DERIVED",
            "source_tier": "INHERITED",
            "derived_from": [
                "share_structure.history",
                "controllers",
                "top_holders",
                "institutional_holdings",
                "shareholder_count",
                "buyback_and_holder_plans",
                "unlocks",
            ],
            "algorithm": "ownership_structural_signals_v1",
        },
    }


def _unified_metadata(section, fetched_at, default_source="DERIVED", default_tier="INHERITED"):
    old = dict(section.get("metadata") or {})
    provenance = section.get("provenance") or {}
    status = str(section.get("status") or "").upper()
    quality = old.get("quality") or (
        "PASS" if status == "OK" else "PARTIAL" if status in {"PARTIAL", "DEFERRED"} else "FAILED"
    )
    source = provenance.get("provider") or default_source
    source_tier = provenance.get("source_tier") or default_tier
    if str(source_tier).upper() == "OFFICIAL":
        freshness_policy = "OFFICIAL_DISCLOSURE"
    elif str(source_tier).upper() == "INHERITED" or str(source).upper() == "DERIVED":
        freshness_policy = "DERIVED"
    else:
        freshness_policy = "PERIODIC_DISCLOSURE"
    value = data_metadata._metadata(
        source,
        provenance.get("fetched_at") or fetched_at,
        data_time=section.get("as_of_date"),
        freshness=old.get("freshness") or "UNKNOWN",
        freshness_policy=freshness_policy,
        quality=quality,
        quality_flags=old.get("quality_flags") or [],
        source_type="API_OR_DERIVED",
        source_tier=source_tier,
    )
    value.update(old)
    section["metadata"] = value


def extend_snapshot(snapshot_path, base, execution_mode):
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    fetched_at = core._runner_time_iso(snapshot)
    as_of = _as_of_date(snapshot)
    detail = snapshot.get("detail_stocks") or {}
    fast = str(execution_mode or "").upper() != "FULL"

    for code, stock in detail.items():
        context = stock.setdefault("ownership_and_capital", {})
        _controller_holding_changes(context)
        if fast:
            capital_tools = _deferred_capital_tools(fetched_at)
        else:
            events, cache = _all_events(code, stock)
            coverage = event_state._coverage(stock, cache, as_of)
            capital_tools = normalize_capital_tools(events, coverage, as_of, fetched_at)
        context["pledges_and_capital_tools"] = capital_tools
        context["valuation_share_semantics"] = _valuation_share_semantics(stock, context, fetched_at)
        _upcoming_events(stock, context, fetched_at)
        context["structural_signals"] = _structural_signals(context)
        context["as_of_date"] = max(
            (
                (context.get(name) or {}).get("as_of_date")
                for name in (
                    "share_structure",
                    "controllers",
                    "top_holders",
                    "institutional_holdings",
                    "shareholder_count",
                    "buyback_and_holder_plans",
                    "unlocks",
                    "pledges_and_capital_tools",
                )
                if (context.get(name) or {}).get("as_of_date")
            ),
            default=None,
        )
        context["provenance"] = {
            "type": "COMPOSITE",
            "derived_from": [
                f"detail_stocks.{code}.quote",
                f"detail_stocks.{code}.events",
                *[
                    f"detail_stocks.{code}.ownership_and_capital.{name}"
                    for name in (
                        "share_structure",
                        "controllers",
                        "top_holders",
                        "institutional_holdings",
                        "shareholder_count",
                        "buyback_and_holder_plans",
                        "unlocks",
                    )
                ],
            ],
            "algorithm": "ownership_and_capital_v1",
        }

        for name in (
            "share_structure",
            "controllers",
            "top_holders",
            "institutional_holdings",
            "shareholder_count",
            "buyback_and_holder_plans",
            "unlocks",
            "pledges_and_capital_tools",
            "valuation_share_semantics",
            "structural_signals",
        ):
            section = context.get(name)
            if isinstance(section, dict):
                _unified_metadata(section, fetched_at)
        _unified_metadata(stock["upcoming_events"], fetched_at, "CNINFO", "OFFICIAL")
        _unified_metadata(context, fetched_at)

        print(
            "OWNERSHIP_ANALYSIS "
            f"{code} capital_tools={capital_tools.get('status')} "
            f"signals={len(context['structural_signals'].get('signals') or [])} "
            f"upcoming={len(stock['upcoming_events'].get('events') or [])} "
            f"valuation={context['valuation_share_semantics'].get('status')}",
            flush=True,
        )

    summary = snapshot.setdefault("ownership_and_capital_summary", {})
    implemented = list(summary.get("implemented_sections") or [])
    for name in ("pledges_and_capital_tools", "valuation_share_semantics", "structural_signals"):
        if name not in implemented:
            implemented.append(name)
    summary["implemented_sections"] = implemented
    summary["signals_contract"] = "AUDITABLE_STRUCTURAL_FACTS_ONLY; UNKNOWN_WHEN_EVIDENCE_INSUFFICIENT"
    summary["capital_tools_contract"] = "CNINFO_OFFICIAL_EVENTS; EXPLICITLY_SCOPED_RATIOS_ONLY"
    summary["integration_contract"] = "COMPANY_EVENTS+UPCOMING_EVENTS+VALUATION_SCOPES+EXACT_HISTORY_CHANGES"
    summary["signal_codes"] = list(SIGNAL_CODES)
    summary["metadata"] = data_metadata._metadata(
        "DERIVED",
        fetched_at,
        freshness="DERIVED_FROM_DISCLOSURES",
        freshness_policy="DERIVED",
        quality="PASS" if summary.get("status") == "OK" else "PARTIAL",
        source_type="DERIVED",
        source_tier="INHERITED",
    )
    summary["provenance"] = {
        "type": "COMPOSITE",
        "derived_from": [f"detail_stocks.{code}.ownership_and_capital" for code in sorted(detail)],
    }
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
