def install(history_store):
    if getattr(history_store, "_capital_flow_history_bridge_installed", False):
        return
    original = history_store._compact_snapshot

    def compact_snapshot(data):
        value = original(data)
        current_detail = data.get("detail_stocks") or {}
        compact_detail = value.get("detail_stocks") or {}
        for code, item in current_detail.items():
            if code in compact_detail and isinstance(item, dict) and "capital_flow" in item:
                compact_detail[code]["capital_flow"] = item.get("capital_flow")
        if "capital_flow_summary" in data:
            value["capital_flow_summary"] = data.get("capital_flow_summary")
        return value

    history_store._compact_snapshot = compact_snapshot
    history_store._capital_flow_history_bridge_installed = True
