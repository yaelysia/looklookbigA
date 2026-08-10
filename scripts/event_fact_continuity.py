import copy


ENRICHED_SCOPE = "ORIGINAL_PDF_TEXT"


def _document_identity(event):
    if not isinstance(event, dict):
        return None
    document_id = event.get("source_document_id")
    if document_id not in (None, ""):
        return ("document_id", str(document_id))
    source_url = event.get("source_url")
    if source_url:
        return ("source_url", str(source_url))
    return None


def _has_successful_pdf_facts(event):
    facts = (event or {}).get("facts") or {}
    document = facts.get("document_extraction") or {}
    return (
        facts.get("extraction_scope") == ENRICHED_SCOPE
        and str(document.get("status") or "").upper() == "OK"
    )


def should_preserve_cached_facts(cached_event, fresh_event):
    if not _has_successful_pdf_facts(cached_event):
        return False
    if _has_successful_pdf_facts(fresh_event):
        return False
    cached_identity = _document_identity(cached_event)
    fresh_identity = _document_identity(fresh_event)
    return bool(cached_identity and cached_identity == fresh_identity)


def preserve_richer_facts(cached_events, fresh_events):
    cached_by_id = {
        str(event.get("event_id")): event
        for event in (cached_events or [])
        if isinstance(event, dict) and event.get("event_id")
    }
    out = []
    for event in fresh_events or []:
        if not isinstance(event, dict):
            out.append(event)
            continue
        value = dict(event)
        cached = cached_by_id.get(str(value.get("event_id")))
        if cached and should_preserve_cached_facts(cached, value):
            value["facts"] = copy.deepcopy(cached.get("facts") or {})
        out.append(value)
    return out


def install(company_events):
    if getattr(company_events, "_event_fact_continuity_installed", False):
        return
    original_merge = company_events._merge_events

    def merge_events(cached_events, fresh_events):
        protected_fresh = preserve_richer_facts(cached_events, fresh_events)
        return original_merge(cached_events, protected_fresh)

    company_events._merge_events = merge_events
    company_events._event_fact_continuity_installed = True
