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
        coverage = context.setdefault("coverage", {})
        coverage["normalized_single_quarter_count"] = len(single)
        coverage["verified_single_quarter_count"] = sum(
            bool((((row or {}).get("normalization") or {}).get("core_fields_verified")))
            for row in single
        )
        coverage["ttm_available"] = ((context.get("ttm") or {}).get("status") == "OK")

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
