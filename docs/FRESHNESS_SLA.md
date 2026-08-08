# Freshness SLA v1

`Freshness SLA` answers a different question from source trust:

> **Is this datum timely enough for the decision that wants to consume it?**

A datum can be authoritative but too old, or very fresh but low-trust. These dimensions remain separate.

The machine-readable policy lives in `scripts/data_policy.py` and is emitted under `snapshot.json.data_policy.freshness_sla`.

## Three measurement modes

Different financial data classes age differently. v1 therefore uses three measurement modes instead of one global `fresh/stale` threshold.

### 1. `DATA_LAG`

Used for continuously changing market-state data:

- realtime quote;
- minute series;
- market breadth;
- future intraday fund flow.

Measurement:

```text
data collection time - source market timestamp
```

Statuses:

```text
<= target_seconds       → MET
<= hard_limit_seconds   → DEGRADED
> hard_limit_seconds    → VIOLATED
missing lag             → UNMEASURED
```

Outside the live trading session, session-close data can remain valid context without pretending to be live. Therefore `CURRENT_SESSION / LAST_SESSION` can produce `NOT_APPLICABLE` for a live-only SLA.

### 2. `DISCOVERY_LAG`

Used for newly published information:

- company announcements;
- regulatory events;
- news;
- industry events;
- macro releases;
- research reports;
- dragon-tiger list publications.

Measurement:

```text
first_seen_at - published_at
```

`fetched_at` must **not** be silently substituted for `first_seen_at` on every run. Otherwise an old event re-fetched today would look like it took weeks to discover.

If a stable `first_seen_at` is unavailable:

```text
status = UNMEASURED
```

This is intentional. Unknown discovery latency is safer than fabricated precision.

### 3. `SESSION_COMPLETENESS`

Used for periodic/session data:

- daily K;
- future daily financing/margin data.

The key question is not "how many seconds old is it?" but:

> Does the dataset contain the latest completed/available trading session it promises?

## v1 policy targets

These values are initial engineering targets, not claims that the current collector always meets them.

| Data class | Measurement | Target | Hard limit / requirement |
|---|---|---:|---:|
| `REALTIME_QUOTE` | DATA_LAG | 60s | 180s |
| `MINUTE_SERIES` | DATA_LAG | 120s | 180s |
| `MARKET_BREADTH` | DATA_LAG | 180s | 600s |
| `INTRADAY_FUND_FLOW` | DATA_LAG | 180s | 600s |
| `COMPANY_EVENT` | DISCOVERY_LAG | 300s | 900s |
| `REGULATORY_EVENT` | DISCOVERY_LAG | 300s | 1800s |
| `NEWS` | DISCOVERY_LAG | 120s | 600s |
| `INDUSTRY_EVENT` | DISCOVERY_LAG | 600s | 1800s |
| `MACRO_RELEASE` | DISCOVERY_LAG | 300s | 900s |
| `RESEARCH_REPORT` | DISCOVERY_LAG | 3600s | 14400s |
| `DRAGON_TIGER_LIST` | DISCOVERY_LAG | 1800s | 7200s |
| `DAILY_K` | SESSION_COMPLETENESS | latest completed bar | max 1 completed session age |
| `DAILY_FINANCING` | SESSION_COMPLETENESS | latest available session | max 1 completed session age |

These thresholds can be revised from evidence, but changes must update the machine-readable policy and regression tests together.

## Decision profiles

A datum may be technically usable while still being too old for a specific strategy.

For example, v1 defines realtime quote profiles:

```text
SHORT_TERM_T      <= 60s
GENERAL_INTRADAY  <= 180s
```

Therefore:

```text
quote lag = 120s

system safety/freshness:
→ not yet hard-stale

SHORT_TERM_T:
→ outside preferred decision SLA
```

This deliberately separates:

- data-integrity guardrails;
- strategy-specific timeliness.

Future #7 logic should use the stricter short-term profile rather than treating every `LIVE` quote as equally suitable for doing-T decisions.

## Metadata contract

Metadata receives a separate block:

```json
{
  "freshness_sla": {
    "model": "freshness_sla_v1",
    "data_class": "REALTIME_QUOTE",
    "measurement": "DATA_LAG",
    "status": "MET",
    "target_seconds": 60,
    "hard_limit_seconds": 180,
    "observed_lag_seconds": 32
  }
}
```

Possible statuses:

```text
MET
DEGRADED
VIOLATED
UNMEASURED
NOT_APPLICABLE
```

SLA status does not replace `quality`.

Examples:

```text
Tier A official announcement + discovery SLA UNMEASURED
→ authoritative fact, but current system cannot prove discovery latency

Tier B realtime quote + SLA MET + quality DEGRADED
→ fresh quote, but perhaps obtained through fallback provider

Tier B quote + SLA VIOLATED
→ source may be valid, but it is too old for live use
```

## Current capability statement

The current architecture is primarily on-demand:

```text
analysis request
→ GitHub Actions
→ collect latest data
→ snapshot
```

Current policy manifest therefore explicitly reports:

```text
realtime_quote            ON_DEMAND_WITH_DATA_LAG_GUARD
minute_series             ON_DEMAND_WITH_DATA_LAG_GUARD
company_event_discovery   ON_DEMAND; discovery lag not yet continuously measured
continuous_watcher        NOT_IMPLEMENTED
```

This is not considered a policy failure. It is a capability statement.

However, adding news/regulatory/industry-event sources without a watcher/first-seen mechanism would leave their core `DISCOVERY_LAG` SLA unmeasured. That should remain visible until a watcher plane exists.

## Requirements for future event/news collectors

Every newly published-information collector should preserve at least:

```text
published_at
first_seen_at
last_seen_at (optional but recommended)
fetched_at
source
source URL/document ID
```

`first_seen_at` must be stable across later refreshes and cache merges.

This allows the system to measure:

```text
discovery_lag_seconds
```

instead of merely claiming to be timely.

## Requirements for future market-data collectors

Every live market-state source should expose:

```text
source market timestamp
data collection timestamp
lag_seconds
freshness state
```

A provider response without a usable source timestamp must not automatically receive a `MET` SLA merely because the HTTP request itself was fast.

## LLM consumption rules

For decision making, read in this order:

1. source trust;
2. SLA status and data class;
3. quality/fallback/divergence flags;
4. decision-profile requirement;
5. factual value itself and its provenance.

For short-term decisions, `VIOLATED` or `UNMEASURED` realtime-market SLA should prevent strong conclusions that depend on current market state.

For historical analysis, a live-data SLA may be `NOT_APPLICABLE` while the same session data remains perfectly usable as historical/session context.
