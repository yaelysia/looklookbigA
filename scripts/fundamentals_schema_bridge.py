def _as_float(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value, digits=4):
    return None if value is None else round(float(value), digits)


def _ratio(a, b):
    a = _as_float(a)
    b = _as_float(b)
    if a is None or b in (None, 0):
        return None
    return a / b


def _series(items, getter, value_key):
    out = []
    for item in sorted(items or [], key=lambda x: x.get("report_period_end") or "")[-8:]:
        value = getter(item)
        if value is None:
            continue
        out.append({
            "period": item.get("period_key"),
            "period_end_date": item.get("report_period_end"),
            value_key: _round(value, 4),
        })
    return out


def _trend_entry(fundamentals_context, series, value_key):
    values = [item.get(value_key) for item in series]
    return {
        "state": fundamentals_context._trend(values),
        "series": series,
        "evidence": series[-3:],
        "method": "deterministic recent-three-point direction/acceleration with volatility guard",
    }


def _same_period_prior(periods, latest):
    if not latest:
        return None
    try:
        prior_year = int(latest["report_period_end"][:4]) - 1
    except (TypeError, ValueError):
        return None
    key = f"{prior_year}{latest.get('period_kind')}"
    return next((x for x in periods if x.get("period_key") == key), None)


def _signal(code, evidence, periods):
    return {
        "status": "ATTENTION",
        "reason_code": code,
        "evidence": evidence,
        "periods": [x for x in periods if x],
        "values": evidence,
        "semantic_note": "Deterministic financial-data divergence context; not a fraud, valuation, or trade conclusion.",
    }


def _standard_signals(context):
    periods = context.get("reported_periods") or []
    singles = context.get("single_quarters") or []
    latest_reported = periods[0] if periods else None
    latest_single = singles[0] if singles else None
    prior_reported = _same_period_prior(periods, latest_reported)
    prior_single = _same_period_prior(singles, latest_single)
    out = []

    for item in context.get("divergence_signals") or []:
        code = item.get("code") if isinstance(item, dict) else None
        if code:
            out.append(_signal(
                code,
                (item or {}).get("evidence") or {},
                [
                    (latest_single or latest_reported or {}).get("report_period_end"),
                    (prior_single or prior_reported or {}).get("report_period_end"),
                ],
            ))

    if latest_reported and prior_reported:
        current_income = latest_reported.get("income") or {}
        current_balance = latest_reported.get("balance_sheet") or {}
        prior_balance = prior_reported.get("balance_sheet") or {}
        revenue_yoy = _as_float(current_income.get("revenue_yoy_percent_reported"))

        def yoy(field):
            current = _as_float(current_balance.get(field))
            previous = _as_float(prior_balance.get(field))
            if current is None or previous in (None, 0):
                return None
            return (current / previous - 1.0) * 100.0

        inventory_yoy = yoy("inventory")
        if revenue_yoy is not None and revenue_yoy > 0 and inventory_yoy is not None and inventory_yoy > revenue_yoy + 15:
            out.append(_signal(
                "REVENUE_UP_INVENTORY_FASTER",
                {"revenue_yoy_percent": _round(revenue_yoy), "inventory_yoy_percent": _round(inventory_yoy)},
                [latest_reported.get("report_period_end"), prior_reported.get("report_period_end")],
            ))

        goodwill_yoy = yoy("goodwill")
        if goodwill_yoy is not None and goodwill_yoy > 25:
            out.append(_signal(
                "GOODWILL_RISING",
                {"goodwill_yoy_percent": _round(goodwill_yoy)},
                [latest_reported.get("report_period_end"), prior_reported.get("report_period_end")],
            ))

        debt_now = _as_float(current_balance.get("debt_to_assets_percent"))
        debt_prior = _as_float(prior_balance.get("debt_to_assets_percent"))
        if debt_now is not None and debt_prior is not None and debt_now > debt_prior + 3.0:
            out.append(_signal(
                "DEBT_RATIO_RISING",
                {"debt_ratio_percent": debt_now, "prior_debt_ratio_percent": debt_prior, "delta_pp": _round(debt_now - debt_prior)},
                [latest_reported.get("report_period_end"), prior_reported.get("report_period_end")],
            ))

        roe_now = _as_float((latest_reported.get("profitability") or {}).get("weighted_roe_percent_reported"))
        roe_prior = _as_float((prior_reported.get("profitability") or {}).get("weighted_roe_percent_reported"))
        if roe_now is not None and roe_prior is not None and roe_now < roe_prior - 1.5:
            out.append(_signal(
                "ROE_DECLINING",
                {"roe_percent": roe_now, "prior_roe_percent": roe_prior, "delta_pp": _round(roe_now - roe_prior)},
                [latest_reported.get("report_period_end"), prior_reported.get("report_period_end")],
            ))

    if latest_single and prior_single:
        margin_now = _as_float((latest_single.get("profitability") or {}).get("net_margin_percent_derived"))
        margin_prior = _as_float((prior_single.get("profitability") or {}).get("net_margin_percent_derived"))
        if margin_now is not None and margin_prior is not None and margin_now < margin_prior - 1.0:
            out.append(_signal(
                "MARGIN_COMPRESSION",
                {"net_margin_percent": margin_now, "prior_net_margin_percent": margin_prior, "delta_pp": _round(margin_now - margin_prior)},
                [latest_single.get("report_period_end"), prior_single.get("report_period_end")],
            ))

    deduped = {item["reason_code"]: item for item in out}
    return [deduped[key] for key in sorted(deduped)]


def _preliminary_events(item):
    events = ((item or {}).get("events") or {}).get("recent") or []
    out = []
    for event in events:
        event_type = str((event or {}).get("event_type") or "")
        if event_type not in {"EARNINGS_FORECAST", "EARNINGS_EXPRESS"}:
            continue
        out.append({
            "event_id": (event or {}).get("event_id"),
            "event_type": event_type,
            "published_at": (event or {}).get("published_at"),
            "status": "PRELIMINARY_OR_FORECAST",
            "semantic_note": "Does not overwrite formally reported financial statement history.",
        })
    return out


def install(fundamentals_context):
    if getattr(fundamentals_context, "_llm_schema_bridge_installed", False):
        return
    original = fundamentals_context._build_context

    def build_context(code, item, raw, cache, urls, errors, now_iso, execution_mode):
        context = original(code, item, raw, cache, urls, errors, now_iso, execution_mode)
        reported = context.get("reported_periods") or []
        singles = context.get("single_quarters") or []

        for index, period in enumerate(reported):
            period["freshness"] = "LATEST_REPORT" if index == 0 else "PREVIOUS_REPORT" if index == 1 else "HISTORICAL"
            period["value_type"] = "REPORTED_CUMULATIVE"
            period["source"] = context.get("source")
        reported_freshness = {
            period.get("report_period_end"): period.get("freshness")
            for period in reported
            if period.get("report_period_end")
        }
        for quarter in singles:
            quarter["freshness"] = reported_freshness.get(quarter.get("report_period_end"), "HISTORICAL")
            quarter["source"] = context.get("source")
            revenue = _as_float((quarter.get("income") or {}).get("revenue"))
            adjusted = _as_float((quarter.get("income") or {}).get("adjusted_net_profit"))
            profitability = quarter.setdefault("profitability", {})
            profitability["adjusted_net_margin_percent_derived"] = (
                _round(_ratio(adjusted, revenue) * 100.0, 4)
                if _ratio(adjusted, revenue) is not None else None
            )

        latest_reported = reported[0] if reported else None
        revenue_growth = _series(singles, lambda x: _as_float((x.get("yoy") or {}).get("revenue_percent")), "yoy_percent")
        profit_growth = _series(singles, lambda x: _as_float((x.get("yoy") or {}).get("parent_net_profit_percent")), "yoy_percent")
        adjusted_growth = _series(singles, lambda x: _as_float((x.get("yoy") or {}).get("adjusted_net_profit_percent")), "yoy_percent")
        cashflow_growth = _series(singles, lambda x: _as_float((x.get("yoy") or {}).get("operating_cash_flow_percent")), "yoy_percent")
        net_margin = _series(singles, lambda x: _as_float((x.get("profitability") or {}).get("net_margin_percent_derived")), "value_percent")
        adjusted_margin = _series(singles, lambda x: _as_float((x.get("profitability") or {}).get("adjusted_net_margin_percent_derived")), "value_percent")
        roe = _series(reported, lambda x: _as_float((x.get("profitability") or {}).get("weighted_roe_percent_reported")), "value_percent")
        gross_margin = _series(reported, lambda x: _as_float((x.get("profitability") or {}).get("gross_margin_percent_reported")), "value_percent")
        debt_ratio = _series(reported, lambda x: _as_float((x.get("balance_sheet") or {}).get("debt_to_assets_percent")), "value_percent")

        context["trends"] = {
            "revenue_growth": _trend_entry(fundamentals_context, revenue_growth, "yoy_percent"),
            "profit_growth": _trend_entry(fundamentals_context, profit_growth, "yoy_percent"),
            "adjusted_profit_growth": _trend_entry(fundamentals_context, adjusted_growth, "yoy_percent"),
            "net_margin": _trend_entry(fundamentals_context, net_margin, "value_percent"),
            "adjusted_net_margin": _trend_entry(fundamentals_context, adjusted_margin, "value_percent"),
            "gross_margin": _trend_entry(fundamentals_context, gross_margin, "value_percent"),
            "roe": _trend_entry(fundamentals_context, roe, "value_percent"),
            "cashflow_growth": _trend_entry(fundamentals_context, cashflow_growth, "yoy_percent"),
            "debt_ratio": _trend_entry(fundamentals_context, debt_ratio, "value_percent"),
        }
        context["signals"] = _standard_signals(context)
        context["quarterly_history"] = singles[:8]
        context["latest_report"] = {
            "report_period": (latest_reported or {}).get("period_key"),
            "report_type": (latest_reported or {}).get("period_kind"),
            "period_end_date": (latest_reported or {}).get("report_period_end"),
            "published_at": (latest_reported or {}).get("published_at"),
            "first_seen_at": (latest_reported or {}).get("first_seen_at"),
            "fetched_at": context.get("fetched_at"),
            "source": context.get("source"),
            "source_urls": ((context.get("provider_health") or {}).get("source_urls") or {}),
            "restated": None,
            "revised": None,
            "revision_status": "UNKNOWN_UNLESS_PROVIDER_OR_OFFICIAL_EVENT_EXPLICITLY_MARKS_REVISION",
        }
        context["income"] = {
            "latest_reported": (latest_reported or {}).get("income"),
            "single_quarter_history": [
                {
                    "period": x.get("period_key"),
                    "period_end_date": x.get("report_period_end"),
                    "freshness": x.get("freshness"),
                    "value_type": "NORMALIZED_SINGLE_QUARTER",
                    "source": context.get("source"),
                    "revenue": (x.get("income") or {}).get("revenue"),
                    "parent_net_profit": (x.get("income") or {}).get("parent_net_profit"),
                    "adjusted_net_profit": (x.get("income") or {}).get("adjusted_net_profit"),
                    "adjusted_net_margin_percent": (x.get("profitability") or {}).get("adjusted_net_margin_percent_derived"),
                    "yoy": x.get("yoy"),
                    "normalization": x.get("normalization"),
                }
                for x in singles[:8]
            ],
        }
        context["profitability"] = {
            "latest_reported": (latest_reported or {}).get("profitability"),
            "gross_margin": context["trends"]["gross_margin"],
            "net_margin": context["trends"]["net_margin"],
            "adjusted_net_margin": context["trends"]["adjusted_net_margin"],
            "roe": context["trends"]["roe"],
            "roa": ((context.get("profitability_context") or {}).get("roa")),
        }
        context["cashflow"] = {
            "latest_reported": (latest_reported or {}).get("cashflow"),
            "quality": context.get("cashflow_quality"),
            "growth_trend": context["trends"]["cashflow_growth"],
        }
        context["balance_sheet"] = {
            "latest_reported": (latest_reported or {}).get("balance_sheet"),
            "growth": context.get("balance_sheet_growth"),
            "debt_ratio_trend": context["trends"]["debt_ratio"],
        }
        context["preliminary_financial_events"] = _preliminary_events(item)
        return context

    fundamentals_context._build_context = build_context
    fundamentals_context._llm_schema_bridge_installed = True
