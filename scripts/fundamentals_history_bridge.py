def install(history_store):
    if getattr(history_store, "_fundamentals_history_bridge_installed", False):
        return
    original = history_store._compact_snapshot

    def compact_snapshot(data):
        value = original(data)
        source_detail = data.get("detail_stocks") or {}
        compact_detail = value.get("detail_stocks") or {}
        for code, item in source_detail.items():
            if code in compact_detail and isinstance(item, dict) and "fundamentals" in item:
                compact_detail[code]["fundamentals"] = item.get("fundamentals")
        if "fundamentals_summary" in data:
            value["fundamentals_summary"] = data.get("fundamentals_summary")
        return value

    history_store._compact_snapshot = compact_snapshot
    history_store._fundamentals_history_bridge_installed = True
