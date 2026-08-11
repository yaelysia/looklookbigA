def _as_float(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value, digits=4):
    return None if value is None else round(float(value), digits)


def _cashflow_quality(latest_single):
    income = (latest_single or {}).get("income") or {}
    cashflow = (latest_single or {}).get("cashflow") or {}
    profit = _as_float(income.get("parent_net_profit"))
    cfo = _as_float(cashflow.get("operating_cash_flow"))
    if profit is None or cfo is None:
        return {
            "state": "UNKNOWN",
            "operating_cash_flow_to_parent_profit": None,
            "reason_codes": ["PROFIT_OR_CFO_UNAVAILABLE"],
        }
    if profit > 0:
        ratio = cfo / profit
        if cfo < 0:
            state = "DIVERGENT"
        elif ratio >= 1.0:
            state = "STRONG"
        elif ratio >= 0.7:
            state = "ADEQUATE"
        else:
            state = "WEAK"
    elif profit < 0 and cfo > 0:
        ratio = cfo / abs(profit)
        state = "CASHFLOW_BETTER_THAN_EARNINGS"
    else:
        ratio = None if profit == 0 else cfo / abs(profit)
        state = "WEAK"
    return {
        "state": state,
        "operating_cash_flow_to_parent_profit": _round(ratio, 4),
        "reason_codes": [f"CASHFLOW_QUALITY_{state}"],
        "semantic_note": "Deterministic accounting cash-conversion context; not a valuation or buy/sell signal.",
    }


def _balance_growth(fundamentals_context, latest, periods):
    if not latest:
        return {"status": "UNAVAILABLE", "reason": "NO_REPORTED_PERIOD"}
    fields = (
        "total_assets",
        "total_liabilities",
        "equity",
        "cash",
        "receivables",
        "inventory",
        "goodwill",
        "interest_bearing_debt",
    )
    growth = {
        field: _round(fundamentals_context._balance_yoy(latest, periods, field), 4)
        for field in fields
    }
    available = sum(value is not None for value in growth.values())
    return {
        "status": "OK" if available else "UNAVAILABLE",
        "comparison": "SAME_PERIOD_PRIOR_YEAR",
        "yoy_percent": growth,
        "available_metric_count": available,
    }


def install(fundamentals_context):
    if getattr(fundamentals_context, "_quality_context_installed", False):
        return
    original = fundamentals_context._build_context

    def build_context(code, item, raw, cache, urls, errors, now_iso, execution_mode):
        context = original(code, item, raw, cache, urls, errors, now_iso, execution_mode)
        periods = context.get("reported_periods") or []
        single = context.get("single_quarters") or []
        latest_reported = periods[0] if periods else None
        latest_single = single[0] if single else None
        context["cashflow_quality"] = _cashflow_quality(latest_single)
        context["balance_sheet_growth"] = _balance_growth(
            fundamentals_context,
            latest_reported,
            periods,
        )
        context["profitability_context"] = {
            "roa": {
                "status": "UNAVAILABLE",
                "value_percent": None,
                "reason": "NO_RELIABLE_AVERAGE_ASSET_DENOMINATOR_IN_V1",
            },
            "roe": {
                "status": "REPORTED_IF_AVAILABLE",
                "value_percent": ((latest_reported or {}).get("profitability") or {}).get("weighted_roe_percent_reported"),
            },
            "gross_margin": {
                "status": "REPORTED_IF_AVAILABLE",
                "value_percent": ((latest_reported or {}).get("profitability") or {}).get("gross_margin_percent_reported"),
            },
            "net_margin_single_quarter": {
                "status": "DERIVED_IF_VERIFIED_SINGLE_QUARTER_AVAILABLE",
                "value_percent": ((latest_single or {}).get("profitability") or {}).get("net_margin_percent_derived"),
            },
        }
        return context

    fundamentals_context._build_context = build_context
    fundamentals_context._quality_context_installed = True
