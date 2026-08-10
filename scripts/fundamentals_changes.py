import json
from pathlib import Path

import history_store


SEVERITY = {"NONE": 0, "MINOR": 1, "MODERATE": 2, "SIGNIFICANT": 3}


def _max(*values):
    best = "NONE"
    for value in values:
        if SEVERITY.get(value, 0) > SEVERITY.get(best, 0):
            best = value
    return best


def _state(before, after):
    comparable = before is not None and after is not None
    return {"before": before, "after": after, "changed": bool(comparable and before != after), "comparable": comparable}


def _trend_state(value):
    return value.get("state") if isinstance(value, dict) else value


def _signal_codes(value):
    signals = (value or {}).get("signals")
    if isinstance(signals, list):
        return {str(x.get("reason_code")) for x in signals if isinstance(x, dict) and x.get("reason_code")}
    return {str(x.get("code")) for x in ((value or {}).get("divergence_signals") or []) if isinstance(x, dict) and x.get("code")}


def _latest_single(value):
    rows = (value or {}).get("single_quarters") or []
    return rows[0] if rows else None


def _metric(item, group, field):
    return ((item or {}).get(group) or {}).get(field)


def _numeric(before, after):
    try:
        b, a = float(before), float(after)
    except (TypeError, ValueError):
        return {"before": before, "after": after, "delta": None, "comparable": False}
    return {"before": b, "after": a, "delta": a - b, "comparable": True}


def _period_progression(before, after):
    if not before or not after:
        return "UNKNOWN"
    if after > before:
        return "ADVANCED"
    if after < before:
        return "REGRESSED"
    return "SAME"


def build_change(before, after):
    if not isinstance(after, dict):
        return {"status": "UNAVAILABLE", "significance": "NONE"}
    if not isinstance(before, dict):
        return {"status": "NO_BASELINE", "significance": "NONE", "reason_codes": ["BASELINE_PREDATES_FUNDAMENTALS"]}

    before_period = before.get("latest_report_period_end")
    after_period = after.get("latest_report_period_end")
    progression = _period_progression(before_period, after_period)
    new_period = progression == "ADVANCED"
    regressed_period = progression == "REGRESSED"
    before_single = _latest_single(before)
    after_single = _latest_single(after)
    same_single_period = bool(before_single and after_single and before_single.get("report_period_end") == after_single.get("report_period_end"))

    trend_keys = (
        "revenue_growth", "profit_growth", "adjusted_profit_growth", "net_margin",
        "adjusted_net_margin", "gross_margin", "roe", "cashflow_growth", "debt_ratio",
    )
    trends = {
        key: _state(_trend_state((before.get("trends") or {}).get(key)), _trend_state((after.get("trends") or {}).get(key)))
        for key in trend_keys
    }
    cashflow_quality = _state(
        ((before.get("cashflow_quality") or {}).get("state")),
        ((after.get("cashflow_quality") or {}).get("state")),
    )
    before_signals, after_signals = _signal_codes(before), _signal_codes(after)
    new_signals = sorted(after_signals - before_signals)
    cleared_signals = sorted(before_signals - after_signals)

    if same_single_period:
        metrics = {
            "revenue": _numeric(_metric(before_single, "income", "revenue"), _metric(after_single, "income", "revenue")),
            "parent_net_profit": _numeric(_metric(before_single, "income", "parent_net_profit"), _metric(after_single, "income", "parent_net_profit")),
            "adjusted_net_profit": _numeric(_metric(before_single, "income", "adjusted_net_profit"), _metric(after_single, "income", "adjusted_net_profit")),
            "operating_cash_flow": _numeric(_metric(before_single, "cashflow", "operating_cash_flow"), _metric(after_single, "cashflow", "operating_cash_flow")),
        }
    elif regressed_period:
        metrics = {"status": "NONCOMPARABLE_REPORT_PERIOD_REGRESSION"}
    else:
        metrics = {"status": "NONCOMPARABLE_NEW_REPORT_PERIOD" if new_period else "UNAVAILABLE"}

    reasons, significance = [], "NONE"
    if new_period:
        reasons.append("NEW_FINANCIAL_REPORT_PERIOD")
        significance = _max(significance, "SIGNIFICANT")
    if regressed_period:
        reasons.append("FINANCIAL_REPORT_PERIOD_REGRESSED")
        significance = _max(significance, "MODERATE")
    if any(value.get("changed") for value in trends.values()) and not regressed_period:
        reasons.append("FUNDAMENTAL_TREND_CHANGED")
        significance = _max(significance, "MODERATE")
    if cashflow_quality.get("changed") and not regressed_period:
        reasons.append("CASHFLOW_QUALITY_CHANGED")
        significance = _max(significance, "MODERATE")
    if new_signals and not regressed_period:
        reasons.append("NEW_FUNDAMENTAL_DIVERGENCE")
        significance = _max(significance, "MODERATE")
    if cleared_signals and not regressed_period:
        reasons.append("FUNDAMENTAL_DIVERGENCE_CLEARED")
        significance = _max(significance, "MINOR")
    if same_single_period and any(
        value.get("comparable") and value.get("delta") not in (0, 0.0)
        for value in metrics.values() if isinstance(value, dict)
    ):
        reasons.append("SAME_PERIOD_FINANCIAL_VALUES_UPDATED")
        significance = _max(significance, "MODERATE")

    period_state = _state(before_period, after_period)
    period_state["progression"] = progression
    return {
        "status": "OK",
        "significance": significance,
        "latest_report_period": period_state,
        "same_single_period_comparison": same_single_period,
        "single_quarter_metrics": metrics,
        "trends": trends,
        "cashflow_quality": cashflow_quality,
        "new_divergence_signals": new_signals,
        "cleared_divergence_signals": cleared_signals,
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
    changed = significant = moderate = minor = 0
    for code, item in (current.get("detail_stocks") or {}).items():
        before = (((previous or {}).get("detail_stocks") or {}).get(code) or {}).get("fundamentals")
        value = build_change(before, (item or {}).get("fundamentals"))
        stock = stock_changes.setdefault(code, {})
        stock["fundamentals"] = value
        severity = value.get("significance") or "NONE"
        changed += int(severity != "NONE")
        significant += int(severity == "SIGNIFICANT")
        moderate += int(severity == "MODERATE")
        minor += int(severity == "MINOR")
        stock["significance"] = _max(stock.get("significance") or "NONE", severity)
    summary = changes.setdefault("summary", {})
    summary.update({
        "fundamentals_changed_stocks": changed,
        "fundamentals_significant": significant,
        "fundamentals_moderate": moderate,
        "fundamentals_minor": minor,
    })
    current.setdefault("features", {})["fundamentals_changes"] = "v1"
    current["schema_version"] = max(int(current.get("schema_version") or 0), 15)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"FUNDAMENTALS_CHANGES changed={changed} significant={significant} moderate={moderate} minor={minor}", flush=True)
