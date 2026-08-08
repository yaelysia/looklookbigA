import json
import tempfile
from pathlib import Path

import company_event_metadata


def _event(event_id, document_status="OK", importance="HIGH"):
    facts = {
        "extraction_scope": "ORIGINAL_PDF_TEXT" if document_status == "OK" else "TITLE_ONLY",
        "document_extraction": {
            "status": document_status,
            "source_url": f"https://static.cninfo.com.cn/{event_id}.PDF",
        },
    }
    if document_status != "OK":
        facts["document_extraction"]["error"] = "forced"
    return {
        "event_id": event_id,
        "event_type": "EARNINGS_FORECAST",
        "title": "2026年半年度业绩预告",
        "published_at": "2026-07-08T09:00:00+08:00",
        "effective_date": None,
        "source": "CNINFO",
        "source_tier": "OFFICIAL",
        "source_url": f"https://static.cninfo.com.cn/{event_id}.PDF",
        "source_document_id": event_id,
        "importance": importance,
        "facts": facts,
        "freshness": "RECENT_30D",
        "fetched_at": "2026-08-07T18:00:00+08:00",
        "status": "OPEN",
        "related_event_id": None,
        "supersedes_event_id": None,
    }


def _snapshot(document_status="OK"):
    latest = _event("evt-1", document_status=document_status)
    # Deliberately use two distinct dict instances with the same stable ID.
    recent = json.loads(json.dumps(latest, ensure_ascii=False))
    return {
        "schema_version": 12,
        "runner_time_cst": "2026-08-07 18:00:00.000",
        "runner_time_utc": "2026-08-07T10:00:00+00:00",
        "detail_stocks": {
            "002558": {
                "events": {
                    "status": "OK",
                    "source": "CNINFO",
                    "source_tier": "OFFICIAL",
                    "lookback_days": 30,
                    "latest": latest,
                    "recent": [recent],
                    "upcoming": [],
                    "cache": {"state": "REFRESHED", "path": "history/events/002558.json"},
                }
            }
        },
        "company_events": {
            "status": "OK",
            "source": "CNINFO",
            "source_tier": "OFFICIAL",
            "fact_enrichment": {
                "status": "OK" if document_status == "OK" else "PARTIAL",
                "selected_event_count": 1,
                "enriched_event_count": 1 if document_status == "OK" else 0,
                "failed_event_count": 0 if document_status == "OK" else 1,
            },
        },
    }


def _run(snapshot):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        company_event_metadata.finalize_snapshot(path)
        return json.loads(path.read_text(encoding="utf-8"))


def test_every_event_representation_gets_metadata():
    data = _run(_snapshot("OK"))
    events = data["detail_stocks"]["002558"]["events"]
    latest = events["latest"]
    recent = events["recent"][0]
    for event in (latest, recent):
        assert event["metadata"]["source_tier"] == "OFFICIAL"
        assert event["metadata"]["quality"] == "PASS"
        assert event["provenance"]["document_id"] == "evt-1"
        assert event["provenance"]["fact_extraction"] == "ORIGINAL_PDF_TEXT"
    assert events["metadata"]["quality"] == "PASS"
    print("PASS every_event_representation_metadata")


def test_failed_high_importance_document_extraction_is_degraded():
    data = _run(_snapshot("UNAVAILABLE"))
    events = data["detail_stocks"]["002558"]["events"]
    for event in (events["latest"], events["recent"][0]):
        assert event["metadata"]["quality"] == "DEGRADED"
        assert "DOCUMENT_FACT_EXTRACTION_UNAVAILABLE" in event["metadata"]["quality_flags"]
    summary = data["company_events"]
    assert summary["metadata"]["quality"] == "DEGRADED"
    assert "FACT_ENRICHMENT_PARTIAL" in summary["metadata"]["quality_flags"]
    print("PASS failed_document_fact_quality")


def main():
    tests = [
        test_every_event_representation_gets_metadata,
        test_failed_high_importance_document_extraction_is_degraded,
    ]
    for test in tests:
        test()
    print(f"COMPANY_EVENT_METADATA_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
