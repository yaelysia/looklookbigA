# Data provenance / freshness / quality schema

`snapshot.json` schema v10 adds a unified metadata contract for LLM and programmatic consumers without removing existing fields.

## Metadata contract

Important raw and derived nodes expose a `metadata` object with the same keys:

```json
{
  "source": "Eastmoney",
  "source_type": "API",
  "source_tier": "PRIMARY_PROVIDER",
  "fetched_at": "2026-08-07T14:30:00+08:00",
  "data_time": "2026-08-07T14:29:58+08:00",
  "lag_seconds": 2,
  "freshness": "LIVE",
  "freshness_policy": "REALTIME_QUOTE",
  "confidence": "HIGH",
  "quality": "PASS",
  "fallback_used": false,
  "quality_flags": []
}
```

`fetched_at` is the snapshot collection time. `data_time` is the timestamp/date represented by the source data. They must not be interpreted as the same timestamp.

## Source tiers

- `OFFICIAL`: statutory/exchange/official disclosure source.
- `PRIMARY_PROVIDER`: preferred market-data provider for the node.
- `SECONDARY_PROVIDER`: fallback/secondary provider.
- `DERIVED`: locally calculated result.
- `CACHE`: persisted historical/cache data.
- `UNKNOWN`: provenance cannot be established reliably.

Current mappings include Eastmoney -> `PRIMARY_PROVIDER`, Tencent -> `SECONDARY_PROVIDER`, history/market-data cache -> `CACHE`, and locally calculated context -> `DERIVED`.

## Quality states

- `PASS`: expected data is present and no important degradation was detected.
- `DEGRADED`: usable, but fallback/divergence/stale-cache/partial peer coverage or another known degradation is present.
- `PARTIAL`: important context is incomplete or freshness cannot be fully established.
- `FAILED`: no valid critical data is available for that node.

`quality_flags` explain why a node is not a clean `PASS`, for example:

```text
PRIMARY_SOURCE_FAILED
FALLBACK_USED
SOURCE_DIVERGENCE
STALE_DATA
HISTORY_CACHE_USED
STALE_CACHE_FALLBACK
PEER_COVERAGE_INCOMPLETE
BREADTH_ESTIMATED
BREADTH_UNAVAILABLE
NO_VALID_DATA
```

## Freshness policies

Freshness is interpreted per data type instead of forcing every dataset into one clock rule:

- `REALTIME_QUOTE`: existing quote freshness such as `LIVE`, `CURRENT_SESSION`, `LAST_SESSION`, `STALE`, `UNAVAILABLE`.
- `MINUTE_SERIES`: minute-series `LIVE`, `STALE`, or `UNAVAILABLE`.
- `DAILY_K_CONTEXT`: `LATEST_COMPLETED_BAR` / unavailable historical context.
- `CACHE_HISTORY`: explicitly `HISTORICAL`; never usable as a current quote source.
- `DERIVED`: inherits/aggregates current input context and is marked as derived.

Future event/financial datasets can add dataset-specific freshness values while preserving the same metadata field names.

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

## Top-level quality summary

Schema v10 adds:

```text
data_quality
├─ overall
├─ critical_failures
├─ warnings
├─ quality_summary
├─ source_summary
└─ freshness_summary

llm_data_summary
├─ critical_data_ready
├─ realtime_quote_quality
├─ market_context_quality
├─ historical_context_quality
├─ overall_data_quality
└─ warnings
```

`critical_data_ready=false` is used when a detail stock has no valid current quote metadata or the live-price guard reports a hard failure. Warnings do not silently become success.

`llm_data_summary` is a compact reading aid. Consumers that need to make trust decisions should inspect the detailed node metadata and provenance as well.

## Compatibility and safety

The metadata finalizer runs after existing quote, intraday, daily-K, history, live-price-guard, resilience and market-environment finalizers. It does not select prices or mutate the existing calculation values.

History/cache nodes are explicitly tiered as `CACHE`/`HISTORICAL`; this metadata layer does not weaken the existing rule that historical/cache data cannot become `quote.latest`.
