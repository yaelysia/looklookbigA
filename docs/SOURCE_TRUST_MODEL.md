# Source Trust Model v1

`Source Trust Model` answers one question:

> **Why should an LLM trust this datum as a fact?**

It is deliberately separate from freshness, provider failover role, and derived-data quality.

## Core rule

Trust is categorical, not a numeric confidence score. A value such as `0.87 trusted` would imply precision the source model cannot justify.

Every source is classified into one of these trust tiers:

| Tier | Class | Intended use |
|---|---|---|
| `A` | `OFFICIAL_ORIGINAL` | authoritative factual baseline |
| `B` | `MARKET_DATA_PROVIDER` | verified market-state data; cross-check when material |
| `C` | `PROFESSIONAL_SECONDARY` | professional interpretation/context |
| `D` | `SECONDARY_SOCIAL` | signal/discovery only; must be corroborated before becoming fact |
| `INHERITED` | `DERIVED_OR_CACHE` | inherits trust from declared provenance |
| `UNKNOWN` | `UNKNOWN` | never assume factual authority |

The policy is machine-readable in `scripts/data_policy.py` and is emitted into `snapshot.json.data_policy`.

## Tier A — official/original

Examples:

- CNINFO official company disclosures;
- SSE / SZSE / BSE;
- CSRC and other regulators;
- central-bank / statistical / government releases;
- company first-party official disclosures;
- court/regulatory original documents.

Policy:

```text
fact_policy = AUTHORITATIVE_FACT
```

Tier A means the source is authoritative for what it directly states. It does **not** mean every interpretation derived from that fact is authoritative.

Example:

```text
CNINFO: company forecasts H1 profit of CNY 2.0–2.2bn
→ authoritative disclosed fact

"therefore the stock must rise"
→ not an official fact; this is analysis
```

## Tier B — market-data provider

Current examples:

- Eastmoney;
- Tencent market data.

Policy:

```text
fact_policy = VERIFIED_MARKET_DATA
```

These are professional market-data sources but are not treated as equivalent to a direct exchange feed. Material discrepancies should be exposed through source consensus / divergence rather than silently hidden.

### Provider role is not trust tier

Existing metadata may say:

```text
source_tier = PRIMARY_PROVIDER
source_tier = SECONDARY_PROVIDER
```

That describes runtime selection/failover role.

It is different from:

```text
trust.tier = B
```

Therefore Eastmoney can be the primary provider and Tencent the fallback while both remain Trust B.

## Tier C — professional secondary

Reserved for future sources such as:

- broker research;
- professional financial databases;
- established financial media when they are reporting/analysing rather than hosting the original filing.

Policy:

```text
fact_policy = SECONDARY_CONTEXT
```

Tier C may be useful for interpretation, expectations, consensus and context, but claims should be linked back to Tier A/B evidence where possible.

## Tier D — secondary/social

Examples may include:

- social media;
- forums;
- self-media;
- unverified reposts;
- rumor-like sources.

Policy:

```text
fact_policy = SIGNAL_ONLY
```

Tier D is useful for discovery and sentiment, not as an uncorroborated factual baseline.

A future news/sentiment layer must never promote Tier D content to an official company fact merely because multiple reposts repeat it.

## Derived and cached data

Derived/cache nodes do not gain trust by being stored inside the repository.

```text
DERIVED
CACHE
→ trust.tier = INHERITED
→ fact_policy = INHERIT_INPUT_TRUST
```

Examples:

- `market_environment` inherits from index/group/breadth inputs;
- `changes_since_previous` inherits from current and baseline snapshots;
- cached daily K inherits the original market-data provenance but has different freshness;
- cached company events retain their original CNINFO authority but must expose cache/freshness status separately.

This avoids a dangerous failure mode:

> "It is in our own history branch, therefore it is trustworthy."

Storage location is not source authority.

## Metadata contract

The policy bridge extends existing metadata with:

```json
{
  "trust": {
    "model": "source_trust_v1",
    "tier": "A",
    "class": "OFFICIAL_ORIGINAL",
    "mode": "DIRECT",
    "fact_policy": "AUTHORITATIVE_FACT"
  }
}
```

Current `source`, `source_type`, `source_tier`, `quality`, `freshness`, and provenance fields remain intact.

## LLM consumption rules

Recommended interpretation order:

1. Read the factual value/event.
2. Check `trust.tier` and `fact_policy`.
3. Check freshness/SLA.
4. Check `quality` and quality flags.
5. Check provenance and source divergence/fallback state.
6. Only then form an investment interpretation.

A high-trust stale datum is still stale. A fresh low-trust rumor is still a rumor. Trust and freshness must never substitute for each other.

## Adding a new source

Before a new source is accepted into the snapshot contract, its implementation should answer:

- Who originated the information?
- Is this the original record or a secondary interpretation?
- What source trust tier applies?
- What factual domain is the source authoritative for?
- Is there a stronger source that should be preferred?
- Does fallback change source authority or only availability?
- Can derived/cache data preserve original provenance?

If these questions cannot be answered, the source defaults to `UNKNOWN` rather than being treated as factual by convenience.
