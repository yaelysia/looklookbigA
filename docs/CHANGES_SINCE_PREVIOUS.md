# `changes_since_previous` snapshot delta schema

Schema v11 adds `snapshot.json.changes_since_previous` for LLMs that need to answer “what changed since the previous observation?” without re-diffing two complete snapshots.

## Baseline selection

The baseline is the snapshot referenced by the existing history manifest **before the current run begins**.

The current run does not advance the manifest until quote/intraday/daily-K/resilience/market-environment/change/provenance enrichment has completed and the final compact archive has been written successfully.

```text
previous manifest
      ↓
load previous fully archived snapshot
      ↓
calculate changes_since_previous
      ↓
apply provenance / quality metadata
      ↓
write current final archive
      ↓
advance manifest to current archive
```

This prevents an in-progress or half-enriched current snapshot from becoming its own comparison baseline.

If no valid previous snapshot exists:

```json
{
  "status": "NO_BASELINE",
  "market": null,
  "stocks": {},
  "groups": {},
  "events": {"status": "NO_BASELINE"}
}
```

No zero delta is fabricated.

Older archived snapshots may predate market-environment or provenance layers. Available fields are still compared, but the result is `PARTIAL` and `baseline.quality_flags` identifies the missing layers.

## Top-level schema

```text
changes_since_previous
├─ status
├─ baseline
├─ thresholds
├─ market
├─ stocks.<code>
├─ groups.<group_id>
├─ events
├─ summary
├─ metadata        # added by data provenance layer
└─ provenance      # added by data provenance layer
```

All numeric differences retain explicit `before`, `after`, `delta`, `delta_percent_of_before`, and `comparable` fields when applicable. State changes retain `before`, `after`, `changed`, and `comparable`.

## Stock changes

Each detail stock includes:

```text
stocks.<code>
├─ price_change
│  ├─ latest
│  ├─ change_percent
│  ├─ high
│  ├─ low
│  └─ amplitude_percent
├─ turnover_change
│  ├─ amount_1e8
│  ├─ incremental_amount_1e8
│  ├─ incremental_amount_per_minute_1e8
│  └─ same_market_session
├─ intraday_change
│  ├─ numeric
│  │  ├─ trend_5m_percent
│  │  ├─ trend_15m_percent
│  │  ├─ trend_30m_percent
│  │  ├─ price_vs_vwap_percent
│  │  ├─ day_range_position_percent
│  │  ├─ volume_strength_ratio_5m
│  │  └─ amount_strength_ratio_5m
│  └─ states
│     ├─ bias
│     ├─ structure
│     └─ above_vwap
├─ relative_strength_change
├─ strength_direction
├─ significance
└─ significance_reasons
```

`amount_1e8` is a cumulative session value. It is only compared when both snapshots are confirmed to belong to the same market session date. Cross-session values are marked non-comparable rather than interpreted as a turnover collapse.

## Group changes

Group changes include mean/median return, breadth, coverage, target-vs-peer mean, and target rank.

`target_rank.rank_improvement` is positive when the target improves its rank:

```text
before rank 4 → after rank 1 → rank_improvement = +3
```

Rank is derived from the target plus the available peer members in each snapshot; peer counts before and after are retained.

## Market changes

Market changes include:

- each available index `change_percent` delta;
- market regime transition;
- style transition and style-spread deltas;
- market breadth ratio deltas;
- breadth count deltas only when the before/after breadth modes are comparable;
- market turnover delta only within the same market session.

If the older baseline predates `market_environment`, `market.available=false` and the stock-level fields that still exist remain usable.

## Event compatibility

The delta layer already understands a future `detail_stocks.<code>.events` structure by stable `event_id`.

It reports:

- `new`: ID appears in current event context but not previous context;
- `updated`: same ID but deterministic event fields/facts changed;
- `closed`: same ID explicitly transitions to `CLOSED`, `COMPLETED`, or `CANCELLED`.

An event disappearing from a rolling lookback window is **not** treated as closed. This is intentional so source/window truncation cannot manufacture a business event lifecycle transition.

## Significance

Significance is deterministic and the thresholds are embedded in every snapshot under `changes_since_previous.thresholds`.

Current levels:

```text
NONE
MINOR
MODERATE
SIGNIFICANT
```

Examples of SIGNIFICANT thresholds:

- stock change-percent delta: `>= 1.50 percentage points`;
- latest-price relative delta: `>= 1.50%`;
- cumulative same-session turnover relative delta: `>= 100%`;
- 5m/15m/30m/VWAP-position metric delta: `>= 1.50`;
- target group-rank move: `>= 4 places`;
- group breadth delta: `>= 50 points`;
- index change-percent delta: `>= 1.20 percentage points`;
- market breadth-score delta: `>= 50 points`;
- relative-strength delta: `>= 1.20 percentage points`.

MINOR/MODERATE boundaries are also present in JSON; LLM consumers do not need to infer hidden thresholds.

State transitions such as intraday bias, market regime, style, or driver-attribution changes are explicitly listed in `significance_reasons`.

## History/archive compatibility

The compact history snapshot contains the fields needed to act as a future baseline, including final market environment, provenance/quality state and future event context. It deliberately does not recursively archive `changes_since_previous` itself, avoiding a chain of nested diffs.

`history.previous_snapshot_path` identifies the comparison baseline used by the current run; `history.archive_path` identifies the current snapshot only after final archival succeeds.
