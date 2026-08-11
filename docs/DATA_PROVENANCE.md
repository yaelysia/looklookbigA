# Data provenance / freshness / quality schema

`snapshot.json` schema v13 extends the unified metadata contract for LLM and programmatic consumers without removing existing fields.

The contract now treats three questions as separate dimensions:

```text
quality        → did this node collect/compute successfully?
trust          → how authoritative is the source for this claim?
freshness_sla  → is the datum timely enough for its declared data class?
```

A high-trust datum can still be stale. A fresh datum can still come from a weak source. A clean calculation can still inherit weak inputs.

## Metadata contract

Important raw and derived nodes expose a `metadata` object with the same core keys:

```json
{
  "source": "Eastmoney",
  "source_type": "API",
  "source_tier": "PRIMARY_PROVIDER",
  "fetched_at": "2026-08-08T10:00:45+08:00",
  "data_time": "2026-08-08T10:00:00+08:00",
  "lag_seconds": 45,
  "freshness": "LIVE",
  "freshness_policy": "REALTIME_QUOTE",
  "confidence": "HIGH",
  "quality": "PASS",
  "fallback_used": false,
  "quality_flags": [],
  "trust": {
    "model": "source_trust_v1",
    "tier": "B",
    "class": "MARKET_DATA_PROVIDER",
    "mode": "DIRECT",
    "fact_policy": "VERIFIED_MARKET_DATA"
  },
  "freshness_sla": {
    "model": "freshness_sla_v1",
    "data_class": "REALTIME_QUOTE",
    "measurement": "DATA_LAG",
    "status": "MET",
    "target_seconds": 60,
    "hard_limit_seconds": 180,
    "observed_lag_seconds": 45
  }
}
```

`fetched_at` is the snapshot collection time. `data_time` is the timestamp/date represented by the source data. They must not be interpreted as the same timestamp.

## Existing source tiers vs source trust

Existing `source_tier` remains a runtime/provider role:

- `OFFICIAL`: statutory/exchange/official disclosure source;
- `PRIMARY_PROVIDER`: preferred market-data provider;
- `SECONDARY_PROVIDER`: fallback/secondary market-data provider;
- `DERIVED`: locally calculated result;
- `CACHE`: persisted historical/cache data;
- `UNKNOWN`: provenance cannot be established reliably.

Source trust is a separate authority model defined in `docs/SOURCE_TRUST_MODEL.md`.

For example:

```text
Eastmoney source_tier = PRIMARY_PROVIDER
Tencent   source_tier = SECONDARY_PROVIDER

but both:
trust.tier = B
trust.class = MARKET_DATA_PROVIDER
```

Provider selection order must not be mistaken for factual authority rank.

Derived/cache nodes use `trust.tier=INHERITED`: they inherit trust from declared provenance rather than becoming trustworthy merely because they were calculated or stored locally.

## Quality states

- `PASS`: expected data is present and no important degradation was detected.
- `DEGRADED`: usable, but fallback/divergence/stale-cache/partial peer coverage or another known degradation is present.
- `PARTIAL`: important context is incomplete, freshness cannot be fully established, or at least one non-critical node has no usable data while critical realtime data is still available.
- `FAILED`: no valid data is available for that node. If the failed node is critical, top-level `data_quality.overall` is also `FAILED`.

`quality_flags` explain why a node is not a clean `PASS`, or describe an otherwise valid session-state condition, for example:

```text
PRIMARY_SOURCE_FAILED
FALLBACK_USED
SOURCE_DIVERGENCE
STALE_DATA
NOT_LIVE_NOW
HISTORY_CACHE_USED
STALE_CACHE_FALLBACK
PEER_COVERAGE_INCOMPLETE
BREADTH_ESTIMATED
BREADTH_UNAVAILABLE
NO_VALID_DATA
```

`NOT_LIVE_NOW` is informational for valid completed-session data. It does not by itself imply `DEGRADED` or `PARTIAL` quality.

## Freshness state vs Freshness SLA

The existing `freshness` field describes the data state, for example:

- `REALTIME_QUOTE`: `LIVE`, `CURRENT_SESSION`, `LAST_SESSION`, `STALE`, `UNAVAILABLE`;
- `MINUTE_SERIES`: `LIVE`, `CURRENT_SESSION`, `LAST_SESSION`, `STALE`;
- `DAILY_K_CONTEXT`: `LATEST_COMPLETED_BAR`;
- cache/history: `HISTORICAL`;
- derived data: `DERIVED_CURRENT`.

`freshness_sla` evaluates whether that state is timely enough for the declared data class. Full policy is in `docs/FRESHNESS_SLA.md`.

The SLA model uses three measurement modes:

```text
DATA_LAG              realtime/minute/market-state data
DISCOVERY_LAG         announcements/news/regulatory/industry events
SESSION_COMPLETENESS  daily/periodic datasets
```

Possible SLA statuses:

```text
MET
DEGRADED
VIOLATED
UNMEASURED
NOT_APPLICABLE
```

Example: a valid Friday close queried on Saturday can remain `quality=PASS` with `freshness=LAST_SESSION`, while its live-only quote SLA is `NOT_APPLICABLE`. It is valid session context, not a live price.

Company-event discovery currently reports `UNMEASURED` until the system stores a stable `first_seen_at`. The policy explicitly refuses to substitute each run's `fetched_at` for discovery time.

## Provenance for derived data

Derived nodes also expose `provenance`.

Example daily context:

```json
{
  "type": "DERIVED",
  "derived_from": ["detail_stocks.002558.daily_context.bars_last_60"],
  "algorithm": "daily_k_context_v1",
  "field_provenance": {
    "moving_averages.ma20": {
      "algorithm": "SMA",
      "period": 20,
      "derived_from": ["daily_k.close"]
    },
    "atr14": {
      "algorithm": "ATR",
      "period": 14,
      "derived_from": ["daily_k.high", "daily_k.low", "daily_k.close"]
    }
  }
}
```

Group summaries keep the peer codes and coverage used by the calculation. Intraday metrics, market environment, live-price guard and quote-resilience summaries identify their upstream nodes and algorithm version.

## Top-level policy and quality summary

Schema v13 adds a machine-readable top-level policy manifest:

```text
data_policy
├─ source_trust_model
├─ freshness_sla
└─ current_capabilities
```

`current_capabilities` explicitly records the architectural limit that realtime collection is currently on-demand and a continuous event/news watcher is not implemented.

Top-level quality reporting now includes policy summaries:

```text
data_quality
├─ overall
├─ critical_failures
├─ noncritical_failures
├─ warnings
├─ quality_summary
├─ source_summary
│  ├─ source_tiers
│  ├─ sources
│  ├─ trust_tiers
│  └─ trust_classes
├─ freshness_summary
├─ policy_versions
├─ freshness_sla_summary
├─ freshness_sla_violations
└─ freshness_sla_unmeasured

llm_data_summary
├─ critical_data_ready
├─ realtime_quote_quality
├─ market_context_quality
├─ historical_context_quality
├─ overall_data_quality
├─ source_trust_model
├─ freshness_sla_model
├─ freshness_sla_violation_count
├─ freshness_sla_unmeasured_count
└─ warnings
```

`critical_data_ready=false` is used when a detail stock has no valid current quote metadata or the live-price guard reports a hard failure.

A `FAILED` node is never silently converted into top-level success:

- critical `FAILED` -> `data_quality.overall=FAILED` and entry in `critical_failures`;
- non-critical `FAILED` -> top-level quality is at least `PARTIAL`, with the node recorded in both `noncritical_failures` and `warnings`.

SLA violations are intentionally reported separately from `quality`. v1 does not silently rewrite every legacy quality state based on the new SLA policy; this avoids changing previously reviewed collection semantics while still exposing timeliness violations to consumers.

Sibling metadata used when a data node is absent, such as `quote_metadata` / `minutes_metadata`, is included in the same quality summary under the logical node path.

## Compatibility and safety

The policy bridge is installed before downstream metadata adapters, so company events, changes analysis, quotes, minute data, history and derived nodes all use the same trust/SLA contract.

The metadata/policy finalizer still does not select prices or alter existing calculation values.

History/cache nodes remain explicitly `CACHE`/`HISTORICAL` and `trust.tier=INHERITED`; this policy layer does not weaken the existing rule that historical/cache data cannot become `quote.latest`.

For adding new sources, use both:

- `docs/SOURCE_TRUST_MODEL.md` — why the source is authoritative;
- `docs/FRESHNESS_SLA.md` — how quickly its data must be observed or refreshed.
