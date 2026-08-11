NORMALIZED_KIND = {
    "Q1": "Q1",
    "H1": "Q2",
    "Q3": "Q3",
    "FY": "Q4",
}
QUARTER_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}


def install(fundamentals_context):
    if getattr(fundamentals_context, "_normalized_quarter_labels_installed", False):
        return
    original_normalize = fundamentals_context._normalize_single_quarters

    def normalize_single_quarters(periods):
        rows = original_normalize(periods)
        for row in rows:
            source_kind = row.get("period_kind")
            normalized_kind = NORMALIZED_KIND.get(source_kind)
            date = row.get("report_period_end")
            if not normalized_kind or not date:
                continue
            row["source_report_kind"] = source_kind
            row["period_kind"] = normalized_kind
            row["period_key"] = f"{date[:4]}{normalized_kind}"
            normalization = row.setdefault("normalization", {})
            normalization["source_report_kind"] = source_kind
            normalization["normalized_period_kind"] = normalized_kind

        # Original YoY keys were based on cumulative report labels (H1/FY).
        # Rebuild them after relabeling the verified single-quarter series.
        by_key = {row.get("period_key"): row for row in rows if row.get("period_key")}
        for row in rows:
            date = row.get("report_period_end")
            kind = row.get("period_kind")
            if not date or kind not in QUARTER_ORDER:
                continue
            prior = by_key.get(f"{int(date[:4]) - 1}{kind}")
            row["yoy"] = {
                "revenue_percent": fundamentals_context._round(
                    fundamentals_context._pct(
                        (row.get("income") or {}).get("revenue"),
                        ((prior or {}).get("income") or {}).get("revenue"),
                    ), 4
                ),
                "parent_net_profit_percent": fundamentals_context._round(
                    fundamentals_context._pct(
                        (row.get("income") or {}).get("parent_net_profit"),
                        ((prior or {}).get("income") or {}).get("parent_net_profit"),
                    ), 4
                ),
                "adjusted_net_profit_percent": fundamentals_context._round(
                    fundamentals_context._pct(
                        (row.get("income") or {}).get("adjusted_net_profit"),
                        ((prior or {}).get("income") or {}).get("adjusted_net_profit"),
                    ), 4
                ),
                "operating_cash_flow_percent": fundamentals_context._round(
                    fundamentals_context._pct(
                        (row.get("cashflow") or {}).get("operating_cash_flow"),
                        ((prior or {}).get("cashflow") or {}).get("operating_cash_flow"),
                    ), 4
                ),
            }
        return rows

    def quarter_ordinal(item):
        date = item.get("report_period_end")
        kind = item.get("period_kind")
        if not date or kind not in QUARTER_ORDER:
            return None
        return int(date[:4]) * 4 + QUARTER_ORDER[kind] - 1

    fundamentals_context._normalize_single_quarters = normalize_single_quarters
    fundamentals_context._quarter_ordinal = quarter_ordinal
    fundamentals_context._normalized_quarter_labels_installed = True
