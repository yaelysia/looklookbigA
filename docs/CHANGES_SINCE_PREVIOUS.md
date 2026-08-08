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

A central rule of this schema is: **before/after values may be retained for auditing even when they are not semantically comparable.** In that case `comparable=false` and the delta fields are deliberately `null`.

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
│  ├─ same_market_session
│  └─ quality_flags
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
│  └─ peer_universe
├─ strength_direction
├─ strength_basis
├─ quality_flags
├─ significance
└─ significance_reasons
```

### Cumulative turnover comparability

`amount_1e8` is a cumulative session value. It is compared **only** when both snapshots have a reliable market-session date and those dates are equal.

```text
both session dates known + equal
    → comparable=true

both dates known + different
    → comparable=false
    → delta=null
    → MARKET_SESSION_RESET

one or both dates unknown/unparseable
    → comparable=false
    → delta=null
    → MARKET_SESSION_UNCONFIRMED
```

Therefore a legacy/partial baseline with no reliable `market_time_cst` cannot manufacture a cumulative-turnover delta. `incremental_amount_1e8` and `incremental_amount_per_minute_1e8` are also null unless the same session has been positively confirmed.

## Peer-relative comparability

Peer-relative metrics are meaningful only when the peer universe itself is comparable. `stocks.<code>.relative_strength_change.peer_universe` and `groups.<group_id>.peer_universe` preserve this context explicitly.

The comparison requires:

- the same target code;
- the same requested peer count;
- enough member metadata to identify the full requested/configured peer universe on both sides;
- the same configured peer-code set;
- the same effective **available** peer-code set.

The output includes the before/after configured and available peer codes, coverage information, `peer_universe_comparable`, and machine-readable quality flags such as:

```text
PEER_SET_CHANGED
PEER_COVERAGE_CHANGED
PEER_REQUESTED_UNIVERSE_CHANGED
PEER_REQUESTED_UNIVERSE_UNCONFIRMED
PEER_TARGET_CHANGED
PEER_GROUP_CHANGED
PEER_CONTEXT_MISSING
```

If the peer universe is not comparable:

- `vs_group_mean_percent` keeps its before/after values but its delta becomes null and `comparable=false`;
- `relative_to_group` is marked `comparable=false` and is not treated as a real transition;
- group mean/median/breadth/target-vs-peer deltas become non-comparable;
- target ranks before/after remain visible for auditing, but `rank_improvement=null` and `target_rank.comparable=false`;
- `RELATIVE_TO_GROUP_CHANGED`, `GROUP_BREADTH_CHANGED`, and `TARGET_GROUP_RANK_CHANGED` are not emitted as significance reasons;
- stock `strength_direction` falls back to the broad-market relative delta when that field is comparable instead of interpreting peer disappearance as stock strength.

`coverage_percent` itself remains comparable because a coverage drop is useful diagnostic evidence explaining why the peer-relative metrics were invalidated.

## Group changes

Group changes include mean/median return, breadth, coverage, target-vs-peer mean, and target rank.

When `peer_universe_comparable=true`, `target_rank.rank_improvement` is positive when the target improves its rank:

```text
before rank 4 → after rank 1 → rank_improvement = +3
```

Rank is derived from the target plus the available peer members in each snapshot. If available peers change, raw ranks are still retained but `rank_improvement` is deliberately null so a transient provider failure cannot be presented as relative-strength improvement.

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

A non-comparable metric is not eligible to create a significance reason. Data-quality changes such as peer coverage may still be visible in status/quality fields without being mislabeled as stock or sector strength.

## History/archive compatibility

The compact history snapshot contains the fields needed to act as a future baseline, including final market environment, provenance/quality state and future event context. It deliberately does not recursively archive `changes_since_previous` itself, avoiding a chain of nested diffs.

`history.previous_snapshot_path` identifies the comparison baseline used by the current run; `history.archive_path` identifies the current snapshot only after final archival succeeds.

`baseline.current_overall_data_quality` is currently a pre-metadata placeholder and may be null because `changes_since_previous` is calculated before the final provenance pass. The authoritative quality for the completed current snapshot is the top-level `data_quality` produced after the changes layer.
