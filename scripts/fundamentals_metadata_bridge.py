def install(data_metadata):
    if getattr(data_metadata, "_fundamentals_metadata_bridge_installed", False):
        return
    original = data_metadata._decorate_detail

    def decorate_detail(snapshot, fetched_at):
        original(snapshot, fetched_at)
        for code, item in (snapshot.get("detail_stocks") or {}).items():
            fundamentals = item.get("fundamentals") if isinstance(item, dict) else None
            fundamentals_meta = (fundamentals or {}).get("metadata") if isinstance(fundamentals, dict) else None
            detail_meta = item.get("metadata") if isinstance(item, dict) else None
            if not isinstance(fundamentals_meta, dict) or not isinstance(detail_meta, dict):
                continue
            combined = data_metadata._composite_quality([
                detail_meta.get("quality"),
                fundamentals_meta.get("quality"),
            ])
            detail_meta["quality"] = combined
            detail_meta["confidence"] = data_metadata._confidence_for_quality(combined)
            provenance = item.setdefault("provenance", {"type": "COMPOSITE", "derived_from": []})
            derived = provenance.setdefault("derived_from", [])
            path = f"detail_stocks.{code}.fundamentals"
            if path not in derived:
                derived.append(path)

    data_metadata._decorate_detail = decorate_detail
    data_metadata._fundamentals_metadata_bridge_installed = True
