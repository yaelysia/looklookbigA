# Structured company events

Schema v12 adds official company-announcement context under each detail stock and a compact top-level `company_events` health summary.

The purpose is to give LLM consumers structured, auditable event facts instead of requiring them to infer company events from price action or scrape arbitrary finance portals.

## Source policy

Version 1 uses **CNINFO / 巨潮资讯** as the primary disclosure source.

- announcement query: official CNINFO disclosure service;
- source documents: `https://static.cninfo.com.cn/...PDF`;
- `source_tier`: `OFFICIAL`;
- transport: HTTPS only.

No finance-media repost is promoted to `OFFICIAL` event facts.

Provider failure and “there were no announcements” are different states:

```text
successful complete query + zero rows
→ status=OK
→ no_events_reason=NO_ANNOUNCEMENTS_IN_WINDOW

provider failed + usable cache
→ status=DEGRADED
→ cache.state=STALE_FALLBACK

provider failed + no usable cache
→ status=ERROR
→ no_events_reason=PROVIDER_FAILED_NO_CACHE
```

An empty `recent` array must therefore be interpreted together with `status`, `provider_health`, `cache`, and `no_events_reason`.

## Configurable lookback

Watchlist config supports:

```json
{
  "event_lookback_days": 30
}
```

Allowed values are exactly `7`, `30`, or `90`; default is `30`. The same security validation applies to repository-default and reusable-workflow caller configs.

## Per-stock JSON

Each `detail_stocks.<code>` receives:

```text
events
├─ status
├─ source / source_tier / source_url
├─ lookback_days
├─ window_start_date / window_end_date
├─ fetched_at
├─ latest
├─ recent[]
├─ upcoming[]
├─ event_context
├─ cache
├─ provider_health
├─ no_events_reason
├─ error
├─ metadata
└─ provenance
```

`event_context` is a compact index:

```text
event_context
├─ count
├─ by_type
├─ high_importance_event_ids
└─ latest_high_importance_event_id
```

The full event objects remain available in `recent`/`upcoming`; the compact context is only an LLM convenience index.

## Event object

Normalized event fields:

```json
{
  "event_id": "cninfo:1225413627",
  "code": "002558",
  "event_type": "EARNINGS_FORECAST",
  "title": "...",
  "published_at": "2026-07-08T...+08:00",
  "effective_date": null,
  "source": "CNINFO",
  "source_tier": "OFFICIAL",
  "source_url": "https://static.cninfo.com.cn/...PDF",
  "source_document_id": "1225413627",
  "importance": "HIGH",
  "facts": {},
  "freshness": "RECENT_30D",
  "fetched_at": "...+08:00",
  "status": "OPEN",
  "related_event_id": null,
  "supersedes_event_id": null,
  "metadata": {},
  "provenance": {}
}
```

When CNINFO provides an announcement ID, `event_id` is `cninfo:<announcementId>`. A deterministic SHA-256 fallback ID is used only if the official ID is absent.

## Event types

Version 1 deterministic title classification includes:

```text
EARNINGS_FORECAST
EARNINGS_EXPRESS
PERIODIC_REPORT
BUYBACK
HOLDER_INCREASE
HOLDER_DECREASE
UNLOCK
PLEDGE
CONVERTIBLE_BOND
PREFERRED_SHARES
REFINANCING
MAJOR_CONTRACT
M&A
DIVIDEND
EQUITY_INCENTIVE
LITIGATION
REGULATORY
TRADING_ANOMALY
SUSPENSION_RESUMPTION
INVESTOR_RELATIONS
OTHER
```

Classification is deterministic and not a sentiment model. It does **not** label announcements as bullish/bearish.

`importance` is a deterministic prioritization hint (`HIGH / MEDIUM / LOW`), not an investment conclusion.

## Fact extraction

Fact extraction has two levels.

### Title / API fields

Every announcement can produce deterministic facts from the title and any CNINFO response snippet that is actually returned:

- monetary amounts;
- percentages;
- explicit dates;
- reporting period where unambiguous;
- selected type-specific fields such as buyback amount/price cap or unlock date.

The object records:

```text
facts.extraction_scope = TITLE_ONLY | TITLE_AND_API_SNIPPET
```

Missing facts remain `null`/empty. They are not guessed.

### Official PDF text

Important structured event types can be enriched from their official CNINFO PDF. The workflow installs a pinned, SHA256-hashed `pypdf` dependency and uses it as the preferred parser. A locally available `pdftotext` remains an optional fallback.

The parser is bounded:

- only `https://static.cninfo.com.cn/` PDFs are accepted;
- maximum PDF size: 8 MiB;
- maximum extracted text retained in memory: 300k characters;
- maximum three PDF-enriched events per detail stock per run;
- raw PDF text is **not** stored in `snapshot.json` or the history cache.

Successful enrichment records:

```json
{
  "extraction_scope": "ORIGINAL_PDF_TEXT",
  "document_extraction": {
    "status": "OK",
    "source_url": "...",
    "parser": "pypdf"
  }
}
```

If parsing/downloading fails, title-level facts are retained and:

```text
document_extraction.status = UNAVAILABLE
```

For a high-importance event this becomes a quality degradation flag rather than a silent `PASS`.

Initial PDF-specific deterministic extractors include:

- earnings forecast/express: profit range, YoY range, EPS range, forecast period when present in recognizable tabular text;
- buyback: amount range and price cap when recognizable.

Generic amount/percentage/date extraction still runs for the other enriched event types. Extraction scope can be extended incrementally without changing the event identity/schema.

## Corrections and follow-up announcements

Corrections/supplements/progress announcements are kept as separate events. The original event is never overwritten.

Where deterministic title matching is strong enough:

```text
related_event_id
supersedes_event_id
```

link the later announcement back to the earlier event.

This allows an LLM to distinguish:

```text
original disclosure
→ correction / supplement
→ later progress
```

instead of losing the original history.

## Cache, historical completeness and incremental refresh

Event cache lives under the existing history root:

```text
history/events/
├─ _cninfo_stock_map.json
├─ 002558.json
└─ 600795.json
```

The stock-code → CNINFO `orgId` map is cached with a seven-day TTL.

Per-stock event cache schema v2 separates **the requested historical window** from **proof that the window was completely covered**. Important cache fields are:

```text
requested_start_date
covered_start_date
coverage_complete
query_status
incomplete_ranges[]
query_diagnostics
```

The semantics are strict:

```text
coverage_complete=true
+ query_status=OK
→ covered_start_date is valid
→ later runs may use INCREMENTAL_OVERLAP

coverage_complete=false
or prior query_status=PARTIAL
→ covered_start_date is null / not trusted
→ next run uses BACKFILL_INCOMPLETE from the full desired window
```

Legacy cache files are migrated conservatively. An old cache with `query_status=PARTIAL` is treated as incomplete even if that old file already contains a `covered_start_date` field. A successful recent query therefore cannot hide an older pagination gap.

### Page failures

If one CNINFO page fails, successfully fetched rows remain usable for the current run, but the result stays `PARTIAL`. The missing page/range is persisted under `incomplete_ranges`, and the next run performs historical backfill instead of switching to the normal seven-day overlap.

### Queries larger than the page cap

`MAX_PAGES=6` remains a per-query safety bound, not a statement that only six pages of history matter. When a requested date range reports more than six pages, the collector recursively splits the date range and queries smaller bounded segments.

```text
30/90 day window exceeds page cap
        ↓
split by date
        ↓
query each bounded segment
        ↓
all segments complete
        → coverage_complete=true

any segment/page still incomplete
        → PARTIAL
        → coverage_complete=false
        → persist missing range
        → BACKFILL_INCOMPLETE next run
```

A single-day range that itself exceeds the cap cannot be safely subdivided; it remains explicitly `PARTIAL` with `PAGE_CAP_EXCEEDED` rather than being silently treated as complete.

Only after a complete historical window has been established does ordinary refresh start with a seven-day overlap around the latest cached announcement. Merged cache records are deduplicated by stable `event_id`, keeping the newest normalized representation for the same official announcement.

This cache is historical/event context only. It is never considered a source for `quote.latest`.

## Integration with `changes_since_previous`

Company events run **before** the snapshot delta layer.

`changes_since_previous.events` therefore reports stable-ID changes:

- `new`: new event ID appears;
- `updated`: deterministic fields/facts for the same ID changed;
- `closed`: same ID explicitly changes to `CLOSED`, `COMPLETED`, or `CANCELLED`.

An event merely disappearing from a rolling lookback window is not interpreted as closure.

## Integration with provenance / quality

Event containers and every event representation (`latest`, `recent`, `upcoming`) receive the schema-v10 metadata contract.

Examples of event quality flags:

```text
SOURCE_URL_MISSING
PUBLISHED_TIME_MISSING
CORRECTION_OR_SUPERSEDING_EVENT
DOCUMENT_FACT_EXTRACTION_UNAVAILABLE
EVENT_QUERY_PARTIAL
EVENT_CACHE_FALLBACK
STALE_EVENT_CACHE_USED
EVENT_PROVIDER_FAILED_NO_USABLE_CACHE
FACT_ENRICHMENT_PARTIAL
```

Top-level `data_quality` therefore incorporates the event layer instead of requiring the LLM to maintain a separate trust model.

## Supply-chain handling

The PDF parser dependency is isolated in:

```text
requirements-event-facts.txt
```

It is version-pinned and SHA256-hash-pinned. Realtime and reusable workflows install it using `--require-hashes --no-deps`. `scripts/test_workflow_security.py` prevents regression to an unpinned install, and Dependabot tracks pip updates.
