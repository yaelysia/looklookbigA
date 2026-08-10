import json
from pathlib import Path

import history_store


STATE_SEVERITY = {"NONE": 0, "MINOR": 1, "MODERATE": 2, "SIGNIFICANT": 3}


def _as_float(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _numeric(before, after, digits=4):
    b = _as_float(before)
    a = _as_float(after)
    delta = a - b if a is not None and b is not None else None
    return {
        "before": None if b is None else round(b, digits),
        "after": None if a is None else round(a, digits),
        "delta": None if delta is None else round(delta, digits),
        "comparable": delta is not None,
    }


def _state(before, after):
    comparable = before is not None and after is not None
    return {
        "before": before,
        "after": after,
        "changed": bool(comparable and before != after),
        "comparable": comparable,
    }


def _max_severity(*values):
    best = "NONE"
    for value in values:
        if STATE_SEVERITY.get(value, 0) > STATE_SEVERITY.get(best, 0):
            best = value
    return best


def _severity_for_rate(delta):
    value = _as_float(delta)
    if value is None:
        return "NONE"
    magnitude = abs(value)
    if magnitude >= 1.0:
        return "SIGNIFICANT"
    if magnitude >= 0.5:
        return "MODERATE"
    if magnitude >= 0.2:
        return "MINOR"
    return "NONE"


def _primary_peer(capital):
    return ((capital or {}).get("peer_context") or {}).get("primary") or {}


def build_change(before, after):
    if not isinstance(after, dict):
        return {"status": "UNAVAILABLE", "significance": "NONE"}
    if not isinstance(before, dict):
        return {
            "status": "NO_BASELINE",
            "significance": "NONE",
            "reason_codes": ["BASELINE_PREDATES_CAPITAL_FLOW"],
        }

    bobs = (before.get("observed") or {}).get("turnover") or {}
    aobs = (after.get("observed") or {}).get("turnover") or {}
    bderived = before.get("derived") or {}
    aderived = after.get("derived") or {}

    rate = _numeric(bobs.get("amount_rate_5m"), aobs.get("amount_rate_5m"), 2)
    rate_ratio = _numeric(bobs.get("amount_rate_vs_baseline"), aobs.get("amount_rate_vs_baseline"), 4)
    pressure = _state(
        ((bderived.get("pressure") or {}).get("net_bias")),
        ((aderived.get("pressure") or {}).get("net_bias")),
    )
    absorption = _state(
        ((bderived.get("absorption") or {}).get("state")),
        ((aderived.get("absorption") or {}).get("state")),
    )
    confirmation = _state(
        ((bderived.get("price_volume_confirmation") or {}).get("state")),
        ((aderived.get("price_volume_confirmation") or {}).get("state")),
    )
    vwap_acceptance = _state(
        ((bderived.get("vwap_acceptance") or {}).get("state")),
        ((aderived.get("vwap_acceptance") or {}).get("state")),
    )

    bpeer = _primary_peer(before)
    apeer = _primary_peer(after)
    peer_comparable = bool(
        (apeer.get("comparability") or {}).get("comparable_to_previous")
        and bpeer.get("peer_universe_signature") == apeer.get("previous_peer_universe_signature")
    )
    if peer_comparable:
        peer_strength = _numeric(bpeer.get("relative_capital_strength"), apeer.get("relative_capital_strength"), 4)
        peer_rank = _numeric(bpeer.get("rank"), apeer.get("rank"), 0)
    else:
        peer_strength = {"before": bpeer.get("relative_capital_strength"), "after": apeer.get("relative_capital_strength"), "delta": None, "comparable": False}
        peer_rank = {"before": bpeer.get("rank"), "after": apeer.get("rank"), "delta": None, "comparable": False}

    bmargin = (before.get("official_delayed") or {}).get("margin") or {}
    amargin = (after.get("official_delayed") or {}).get("margin") or {}
    new_margin_session = bool(
        amargin.get("as_of_trade_date")
        and bmargin.get("as_of_trade_date")
        and amargin.get("as_of_trade_date") != bmargin.get("as_of_trade_date")
    )
    margin = {
        "before_trade_date": bmargin.get("as_of_trade_date"),
        "after_trade_date": amargin.get("as_of_trade_date"),
        "new_disclosed_session": new_margin_session,
        "financing_balance": _numeric(bmargin.get("financing_balance"), amargin.get("financing_balance"), 2) if new_margin_session else {
            "before": bmargin.get("financing_balance"),
            "after": amargin.get("financing_balance"),
            "delta": None,
            "comparable": False,
        },
    }

    reasons = []
    severity = _max_severity(_severity_for_rate(rate_ratio.get("delta")))
    if pressure.get("changed"):
        reasons.append("PRESSURE_BIAS_CHANGED")
        severity = _max_severity(severity, "MODERATE")
    if absorption.get("changed"):
        reasons.append("ABSORPTION_STATE_CHANGED")
        severity = _max_severity(severity, "MODERATE")
    if confirmation.get("changed"):
        reasons.append("PRICE_VOLUME_CONFIRMATION_CHANGED")
        severity = _max_severity(severity, "MODERATE")
    if vwap_acceptance.get("changed"):
        reasons.append("VWAP_ACCEPTANCE_CHANGED")
        severity = _max_severity(severity, "MINOR")
    if peer_strength.get("comparable") and abs(_as_float(peer_strength.get("delta")) or 0.0) >= 0.5:
        reasons.append("RELATIVE_CAPITAL_STRENGTH_CHANGED")
        severity = _max_severity(severity, "MODERATE")
    if not peer_comparable and (bpeer or apeer):
        reasons.append("PEER_UNIVERSE_NONCOMPARABLE")
    if new_margin_session:
        reasons.append("NEW_MARGIN_DISCLOSURE")
        severity = _max_severity(severity, "MINOR")

    return {
        "status": "OK",
        "significance": severity,
        "turnover": {
            "amount_rate_5m": rate,
            "amount_rate_vs_baseline": rate_ratio,
        },
        "pressure": pressure,
        "absorption": absorption,
        "price_volume_confirmation": confirmation,
        "vwap_acceptance": vwap_acceptance,
        "peer_context": {
            "comparable": peer_comparable,
            "relative_capital_strength": peer_strength,
            "rank": peer_rank,
        },
        "margin": margin,
        "reason_codes": reasons,
    }


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    current = json.loads(path.read_text(encoding="utf-8"))
    previous, _ = history_store.load_previous_snapshot(current)
    changes = current.get("changes_since_previous")
    if not isinstance(changes, dict):
        return
    stock_changes = changes.setdefault("stocks", {})
    changed_count = 0
    significant = 0
    moderate = 0
    minor = 0
    for code, item in (current.get("detail_stocks") or {}).items():
        before = (((previous or {}).get("detail_stocks") or {}).get(code) or {}).get("capital_flow")
        after = (item or {}).get("capital_flow")
        value = build_change(before, after)
        stock = stock_changes.setdefault(code, {})
        stock["capital_flow"] = value
        severity = value.get("significance") or "NONE"
        if severity != "NONE":
            changed_count += 1
        if severity == "SIGNIFICANT":
            significant += 1
        elif severity == "MODERATE":
            moderate += 1
        elif severity == "MINOR":
            minor += 1
        existing = stock.get("significance") or "NONE"
        stock["significance"] = _max_severity(existing, severity)

    summary = changes.setdefault("summary", {})
    summary["capital_flow_changed_stocks"] = changed_count
    summary["capital_flow_significant"] = significant
    summary["capital_flow_moderate"] = moderate
    summary["capital_flow_minor"] = minor
    current.setdefault("features", {})["capital_flow_changes"] = "v1"
    current["schema_version"] = max(int(current.get("schema_version") or 0), 14)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "CAPITAL_FLOW_CHANGES "
        f"changed={changed_count} significant={significant} moderate={moderate} minor={minor}",
        flush=True,
    )
