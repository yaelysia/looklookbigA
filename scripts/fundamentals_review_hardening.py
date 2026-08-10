CORE_TTM_FIELDS = (
    ("income", "revenue", "revenue"),
    ("income", "parent_net_profit", "parent_net_profit"),
    ("income", "adjusted_net_profit", "adjusted_net_profit"),
    ("cashflow", "operating_cash_flow", "operating_cash_flow"),
)


def _field_value(row, group, field):
    return ((row or {}).get(group) or {}).get(field)


def _field_verification(row, fundamentals_context):
    result = {}
    for group, field, key in CORE_TTM_FIELDS:
        result[key] = fundamentals_context._as_float(_field_value(row, group, field)) is not None
    return result


def _ttm_field(selected, fundamentals_context, group, field, key):
    values = [fundamentals_context._as_float(_field_value(row, group, field)) for row in selected]
    verified = [
        bool((((row or {}).get("normalization") or {}).get("verified_fields") or {}).get(key))
        for row in selected
    ]
    available = all(value is not None for value in values) and all(verified)
    return {
        "value": sum(values) if available else None,
        "status": "OK" if available else "UNAVAILABLE",
        "verified_quarter_count": sum(bool(value is not None and flag) for value, flag in zip(values, verified)),
        "required_quarter_count": 4,
        "source_periods": [row.get("report_period_end") for row in selected],
    }


def _same_period_prior(periods, item):
    if not item or not item.get("report_period_end") or not item.get("period_kind"):
        return None
    try:
        prior_year = int(item["report_period_end"][:4]) - 1
    except (TypeError, ValueError):
        return None
    key = f"{prior_year}{item.get('period_kind')}"
    return next((row for row in periods or [] if row.get("period_key") == key), None)


def _same_period_delta_series(periods, getter, fundamentals_context):
    out = []
    for item in sorted(periods or [], key=lambda x: x.get("report_period_end") or "")[-8:]:
        prior = _same_period_prior(periods, item)
        current_value = fundamentals_context._as_float(getter(item))
        prior_value = fundamentals_context._as_float(getter(prior)) if prior else None
        if current_value is None or prior_value is None:
            continue
        out.append({
            "period": item.get("period_key"),
            "period_end_date": item.get("report_period_end"),
            "value_percent": fundamentals_context._round(current_value, 4),
            "prior_year_value_percent": fundamentals_context._round(prior_value, 4),
            "yoy_delta_pp": fundamentals_context._round(current_value - prior_value, 4),
            "comparison": "SAME_REPORT_KIND_PRIOR_YEAR",
        })
    return out


def _direction_state(series, value_key="value_percent"):
    values = [float(item[value_key]) for item in series if item.get(value_key) is not None]
    if len(values) < 3:
        return "UNKNOWN"
    recent = values[-3:]
    changes = [recent[index] - recent[index - 1] for index in range(1, len(recent))]
    scale = max(sum(abs(value) for value in recent) / len(recent), 1e-9)
    tolerance = max(scale * 0.005, 0.05)
    if all(abs(change) <= tolerance for change in changes):
        return "STABLE"
    if all(change > tolerance for change in changes):
        return "RISING"
    if all(change < -tolerance for change in changes):
        return "FALLING"
    return "VOLATILE"


def _same_period_trend(periods, getter, fundamentals_context):
    series = _same_period_delta_series(periods, getter, fundamentals_context)
    values = [item.get("yoy_delta_pp") for item in series]
    return {
        "state": fundamentals_context._trend(values),
        "series": series,
        "evidence": series[-3:],
        "comparability": "SAME_REPORT_KIND_PRIOR_YEAR_ONLY",
        "method": "trend of same-report-kind year-over-year percentage-point deltas; mixed cumulative report kinds are never compared as adjacent levels",
    }


def _point_in_time_direction(periods, getter, fundamentals_context):
    series = []
    for item in sorted(periods or [], key=lambda x: x.get("report_period_end") or "")[-8:]:
        value = fundamentals_context._as_float(getter(item))
        if value is None:
            continue
        series.append({
            "period": item.get("period_key"),
            "period_end_date": item.get("report_period_end"),
            "value_percent": fundamentals_context._round(value, 4),
        })
    return {
        "state": _direction_state(series),
        "series": series,
        "evidence": series[-3:],
        "comparability": "POINT_IN_TIME_SEQUENTIAL",
        "method": "directional point-in-time trend; state is RISING/FALLING/STABLE/VOLATILE/UNKNOWN and carries no good/bad judgement",
    }


def install(fundamentals_context):
    if getattr(fundamentals_context, "_review_hardening_installed", False):
        return

    original_normalize = fundamentals_context._normalize_single_quarters
    original_build_context = fundamentals_context._build_context

    def normalize_single_quarters(periods):
        rows = original_normalize(periods)
        for row in rows:
            normalization = row.setdefault("normalization", {})
            normalization["period_verified"] = bool(normalization.get("verified"))
            normalization["verification_scope"] = "PERIOD_ARITHMETIC_ONLY"
            normalization["verified_fields"] = _field_verification(row, fundamentals_context)
            normalization["core_fields_verified"] = all(normalization["verified_fields"].values())
        return rows

    def ttm(single_quarters):
        ordered = sorted(single_quarters or [], key=lambda x: x.get("report_period_end") or "")
        if len(ordered) < 4:
            return {
                "status": "UNAVAILABLE",
                "reason": "FEWER_THAN_4_NORMALIZED_SINGLE_QUARTERS",
                "field_availability": {},
            }
        selected = ordered[-4:]
        ordinals = [fundamentals_context._quarter_ordinal(row) for row in selected]
        if any(value is None for value in ordinals) or any(ordinals[index] + 1 != ordinals[index + 1] for index in range(3)):
            return {
                "status": "UNAVAILABLE",
                "reason": "NON_CONSECUTIVE_NORMALIZED_SINGLE_QUARTERS",
                "source_periods": [row.get("report_period_end") for row in selected],
                "field_availability": {},
            }

        fields = {
            key: _ttm_field(selected, fundamentals_context, group, field, key)
            for group, field, key in CORE_TTM_FIELDS
        }
        missing = [key for key, value in fields.items() if value["status"] != "OK"]
        ok_count = len(fields) - len(missing)
        status = "OK" if not missing else "PARTIAL" if ok_count else "UNAVAILABLE"
        reason = None if status == "OK" else "CORE_TTM_FIELDS_INCOMPLETE"

        revenue = fields["revenue"]["value"]
        profit = fields["parent_net_profit"]["value"]
        adjusted = fields["adjusted_net_profit"]["value"]
        cfo = fields["operating_cash_flow"]["value"]
        return {
            "status": status,
            "reason": reason,
            "reported_scope": "TTM",
            "through_period_end": selected[-1].get("report_period_end"),
            "source_periods": [row.get("report_period_end") for row in selected],
            "income": {
                "revenue": revenue,
                "parent_net_profit": profit,
                "adjusted_net_profit": adjusted,
            },
            "cashflow": {"operating_cash_flow": cfo},
            "profitability": {
                "net_margin_percent_derived": (
                    fundamentals_context._round(fundamentals_context._ratio(profit, revenue) * 100.0, 4)
                    if fundamentals_context._ratio(profit, revenue) is not None else None
                ),
                "operating_cash_flow_to_parent_profit": fundamentals_context._round(
                    fundamentals_context._ratio(cfo, profit), 4
                ),
            },
            "field_availability": fields,
            "missing_core_fields": missing,
            "verification_contract": "FOUR_CONSECUTIVE_QUARTERS_AND_FIELD_LEVEL_AVAILABILITY",
        }

    def build_context(code, item, raw, cache, urls, errors, now_iso, execution_mode):
        context = original_build_context(code, item, raw, cache, urls, errors, now_iso, execution_mode)
        single = context.get("single_quarters") or []
        reported = context.get("reported_periods") or []
        coverage = context.setdefault("coverage", {})
        coverage["normalized_single_quarter_count"] = len(single)
        coverage["verified_single_quarter_count"] = sum(
            bool((((row or {}).get("normalization") or {}).get("core_fields_verified")))
            for row in single
        )
        coverage["ttm_available"] = ((context.get("ttm") or {}).get("status") == "OK")

        trends = context.get("trends") or {}
        trends["roe"] = _same_period_trend(
            reported,
            lambda row: ((row or {}).get("profitability") or {}).get("weighted_roe_percent_reported"),
            fundamentals_context,
        )
        trends["gross_margin"] = _same_period_trend(
            reported,
            lambda row: ((row or {}).get("profitability") or {}).get("gross_margin_percent_reported"),
            fundamentals_context,
        )
        trends["debt_ratio"] = _point_in_time_direction(
            reported,
            lambda row: ((row or {}).get("balance_sheet") or {}).get("debt_to_assets_percent"),
            fundamentals_context,
        )
        context["trends"] = trends
        if isinstance(context.get("profitability"), dict):
            context["profitability"]["roe"] = trends["roe"]
            context["profitability"]["gross_margin"] = trends["gross_margin"]
        if isinstance(context.get("balance_sheet"), dict):
            context["balance_sheet"]["debt_ratio_trend"] = trends["debt_ratio"]

        if context.get("status") == "CACHED":
            metadata = context.get("metadata") or {}
            report_rows = coverage.get("provider_report_rows") or {}
            complete_classes = all(int(report_rows.get(key) or 0) > 0 for key in fundamentals_context.REPORTS)
            refresh_due = bool(((context.get("refresh_trigger") or {}).get("recommended")))
            provider_errors = ((context.get("provider_health") or {}).get("errors") or [])
            flags = list(metadata.get("quality_flags") or [])
            if "FAST_CACHE_ONLY" not in flags:
                flags.append("FAST_CACHE_ONLY")
            if complete_classes and not refresh_due and not provider_errors:
                metadata["quality"] = "PASS"
                metadata["confidence"] = "HIGH"
            else:
                metadata["quality"] = "DEGRADED"
                metadata["confidence"] = "MEDIUM"
                if refresh_due and "PERIODIC_REPORT_EVENT_AFTER_CACHE" not in flags:
                    flags.append("PERIODIC_REPORT_EVENT_AFTER_CACHE")
                if not complete_classes and "INCOMPLETE_REPORT_CLASS_COVERAGE" not in flags:
                    flags.append("INCOMPLETE_REPORT_CLASS_COVERAGE")
            metadata["quality_flags"] = flags
            context["metadata"] = metadata
        return context

    fundamentals_context._normalize_single_quarters = normalize_single_quarters
    fundamentals_context._ttm = ttm
    fundamentals_context._build_context = build_context
    fundamentals_context._review_hardening_installed = True
