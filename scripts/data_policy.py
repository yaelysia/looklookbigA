from datetime import datetime


SOURCE_TRUST_MODEL_VERSION = "source_trust_v1"
FRESHNESS_SLA_VERSION = "freshness_sla_v1"

# Trust is categorical by design. Numeric scores would imply precision that the
# source model cannot justify.
TRUST_TIERS = {
    "A": {
        "class": "OFFICIAL_ORIGINAL",
        "fact_policy": "AUTHORITATIVE_FACT",
        "description": "Original disclosure or first-party official source.",
    },
    "B": {
        "class": "MARKET_DATA_PROVIDER",
        "fact_policy": "VERIFIED_MARKET_DATA",
        "description": "Professional market-data provider; cross-check when material.",
    },
    "C": {
        "class": "PROFESSIONAL_SECONDARY",
        "fact_policy": "SECONDARY_CONTEXT",
        "description": "Professional interpretation such as broker research or established financial media.",
    },
    "D": {
        "class": "SECONDARY_SOCIAL",
        "fact_policy": "SIGNAL_ONLY",
        "description": "Unverified secondary/social source; never promote to fact without corroboration.",
    },
    "INHERITED": {
        "class": "DERIVED_OR_CACHE",
        "fact_policy": "INHERIT_INPUT_TRUST",
        "description": "Derived/cache data inherits trust from declared provenance, not from its container.",
    },
    "UNKNOWN": {
        "class": "UNKNOWN",
        "fact_policy": "DO_NOT_ASSUME_FACT",
        "description": "Source authority is unknown.",
    },
}

OFFICIAL_SOURCES = {
    "CNINFO",
    "SSE",
    "SZSE",
    "BSE",
    "CSRC",
    "PBOC",
    "NBS",
    "COMPANY_OFFICIAL",
    "GOVERNMENT_OFFICIAL",
    "COURT_OFFICIAL",
}
MARKET_DATA_SOURCES = {"EASTMONEY", "TENCENT"}

# v1 targets are policy targets, not claims that the current collector always
# meets them. In particular, event/news discovery needs a stable first_seen_at
# timestamp before discovery latency can be measured.
FRESHNESS_POLICIES = {
    "REALTIME_QUOTE": {
        "measurement": "DATA_LAG",
        "target_seconds": 60,
        "hard_limit_seconds": 180,
        "market_hours_only": True,
        "decision_profiles": {
            "SHORT_TERM_T": 60,
            "GENERAL_INTRADAY": 180,
        },
    },
    "MINUTE_SERIES": {
        "measurement": "DATA_LAG",
        "target_seconds": 120,
        "hard_limit_seconds": 180,
        "market_hours_only": True,
        "decision_profiles": {
            "SHORT_TERM_T": 120,
            "GENERAL_INTRADAY": 180,
        },
    },
    "MARKET_BREADTH": {
        "measurement": "DATA_LAG",
        "target_seconds": 180,
        "hard_limit_seconds": 600,
        "market_hours_only": True,
    },
    "INTRADAY_FUND_FLOW": {
        "measurement": "DATA_LAG",
        "target_seconds": 180,
        "hard_limit_seconds": 600,
        "market_hours_only": True,
    },
    "DAILY_K": {
        "measurement": "SESSION_COMPLETENESS",
        "required_state": "LATEST_COMPLETED_BAR",
        "max_completed_session_age": 1,
    },
    "COMPANY_EVENT": {
        "measurement": "DISCOVERY_LAG",
        "target_seconds": 300,
        "hard_limit_seconds": 900,
        "requires_first_seen_at": True,
    },
    "REGULATORY_EVENT": {
        "measurement": "DISCOVERY_LAG",
        "target_seconds": 300,
        "hard_limit_seconds": 1800,
        "requires_first_seen_at": True,
    },
    "NEWS": {
        "measurement": "DISCOVERY_LAG",
        "target_seconds": 120,
        "hard_limit_seconds": 600,
        "requires_first_seen_at": True,
    },
    "INDUSTRY_EVENT": {
        "measurement": "DISCOVERY_LAG",
        "target_seconds": 600,
        "hard_limit_seconds": 1800,
        "requires_first_seen_at": True,
    },
    "MACRO_RELEASE": {
        "measurement": "DISCOVERY_LAG",
        "target_seconds": 300,
        "hard_limit_seconds": 900,
        "requires_first_seen_at": True,
    },
    "RESEARCH_REPORT": {
        "measurement": "DISCOVERY_LAG",
        "target_seconds": 3600,
        "hard_limit_seconds": 14400,
        "requires_first_seen_at": True,
    },
    "DRAGON_TIGER_LIST": {
        "measurement": "DISCOVERY_LAG",
        "target_seconds": 1800,
        "hard_limit_seconds": 7200,
        "requires_first_seen_at": True,
    },
    "DAILY_FINANCING": {
        "measurement": "SESSION_COMPLETENESS",
        "required_state": "LATEST_AVAILABLE_SESSION",
        "max_completed_session_age": 1,
    },
    "DERIVED": {
        "measurement": "INHERITED",
    },
    "HISTORICAL_CACHE": {
        "measurement": "INHERITED",
    },
    "PERIODIC_DISCLOSURE": {
        "measurement": "INHERITED",
    },
}

FRESHNESS_POLICY_TO_DATA_CLASS = {
    "REALTIME_QUOTE": "REALTIME_QUOTE",
    "MINUTE_SERIES": "MINUTE_SERIES",
    "DAILY_K_CONTEXT": "DAILY_K",
    "OFFICIAL_DISCLOSURE": "COMPANY_EVENT",
    "OFFICIAL_DISCLOSURE_SET": "COMPANY_EVENT",
    "EVENT_SUMMARY": "DERIVED",
    "DERIVED": "DERIVED",
    "CACHE_HISTORY": "HISTORICAL_CACHE",
    "PERIODIC_DISCLOSURE": "PERIODIC_DISCLOSURE",
}


def _parse_dt(value):
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def source_trust(source, source_type=None, source_tier=None):
    source_text = str(source or "UNKNOWN")
    source_key = source_text.upper()
    source_type_key = str(source_type or "").upper()
    source_tier_key = str(source_tier or "").upper()

    if source_type_key == "DERIVED" or source_tier_key == "DERIVED":
        tier = "INHERITED"
        mode = "DERIVED"
    elif source_type_key == "CACHE" or source_tier_key == "CACHE" or any(
        token in source_text.lower() for token in ("history", "cache", "market-data")
    ):
        tier = "INHERITED"
        mode = "CACHE"
    elif source_key in OFFICIAL_SOURCES or source_tier_key == "OFFICIAL":
        tier = "A"
        mode = "DIRECT"
    elif source_key in MARKET_DATA_SOURCES or source_tier_key in {
        "PRIMARY_PROVIDER",
        "SECONDARY_PROVIDER",
    }:
        tier = "B"
        mode = "DIRECT"
    elif source_tier_key in {"PROFESSIONAL_SECONDARY", "RESEARCH", "MEDIA_PROFESSIONAL"}:
        tier = "C"
        mode = "DIRECT"
    elif source_tier_key in {"SECONDARY", "SOCIAL", "UNVERIFIED"}:
        tier = "D"
        mode = "DIRECT"
    else:
        tier = "UNKNOWN"
        mode = "UNKNOWN"

    policy = TRUST_TIERS[tier]
    return {
        "model": SOURCE_TRUST_MODEL_VERSION,
        "tier": tier,
        "class": policy["class"],
        "mode": mode,
        "fact_policy": policy["fact_policy"],
    }


def _lag_status(lag_seconds, target_seconds, hard_limit_seconds):
    if lag_seconds is None:
        return "UNMEASURED"
    lag = max(0, float(lag_seconds))
    if lag <= target_seconds:
        return "MET"
    if lag <= hard_limit_seconds:
        return "DEGRADED"
    return "VIOLATED"


def evaluate_freshness_sla(
    data_class,
    freshness=None,
    lag_seconds=None,
    data_time=None,
    fetched_at=None,
    first_seen_at=None,
    session_verified=None,
    completed_session_age=None,
    session_validation_state=None,
):
    policy = FRESHNESS_POLICIES.get(data_class)
    if not policy:
        return {
            "model": FRESHNESS_SLA_VERSION,
            "data_class": data_class or "UNKNOWN",
            "measurement": "UNKNOWN",
            "status": "UNMEASURED",
            "reason": "NO_POLICY",
        }

    measurement = policy["measurement"]
    result = {
        "model": FRESHNESS_SLA_VERSION,
        "data_class": data_class,
        "measurement": measurement,
        "status": "UNMEASURED",
        "reason": None,
    }
    for key in (
        "target_seconds",
        "hard_limit_seconds",
        "market_hours_only",
        "max_completed_session_age",
        "required_state",
        "decision_profiles",
    ):
        if key in policy:
            result[key] = policy[key]

    if measurement == "INHERITED":
        result["status"] = "NOT_APPLICABLE"
        result["reason"] = "INHERIT_FROM_INPUT_PROVENANCE"
        return result

    if measurement == "DATA_LAG":
        if policy.get("market_hours_only") and freshness in {"CURRENT_SESSION", "LAST_SESSION"}:
            result["status"] = "NOT_APPLICABLE"
            result["reason"] = "OUTSIDE_LIVE_SESSION"
            return result
        status = _lag_status(
            lag_seconds,
            policy["target_seconds"],
            policy["hard_limit_seconds"],
        )
        if freshness == "STALE":
            status = "VIOLATED"
        result["status"] = status
        result["observed_lag_seconds"] = lag_seconds
        if status == "UNMEASURED":
            result["reason"] = "LAG_MISSING"
        return result

    if measurement == "DISCOVERY_LAG":
        published = _parse_dt(data_time)
        first_seen = _parse_dt(first_seen_at)
        if not published or not first_seen:
            result["status"] = "UNMEASURED"
            result["reason"] = "FIRST_SEEN_AT_OR_PUBLISHED_AT_MISSING"
            return result
        if published.tzinfo is None and first_seen.tzinfo is not None:
            published = published.replace(tzinfo=first_seen.tzinfo)
        if first_seen.tzinfo is None and published.tzinfo is not None:
            first_seen = first_seen.replace(tzinfo=published.tzinfo)
        lag = max(0, int((first_seen - published).total_seconds()))
        result["observed_discovery_lag_seconds"] = lag
        result["status"] = _lag_status(
            lag,
            policy["target_seconds"],
            policy["hard_limit_seconds"],
        )
        return result

    if measurement == "SESSION_COMPLETENESS":
        required = policy.get("required_state")
        result["observed_data_time"] = data_time
        result["evaluated_at"] = fetched_at
        result["session_verified"] = bool(session_verified) if session_verified is not None else None
        result["observed_completed_session_age"] = completed_session_age
        result["session_validation_state"] = session_validation_state

        if not _parse_dt(data_time):
            result["status"] = "UNMEASURED"
            result["reason"] = "DATA_TIME_MISSING_OR_INVALID"
            return result
        if session_verified is not True:
            result["status"] = "UNMEASURED"
            result["reason"] = "SESSION_COMPLETENESS_UNVERIFIED"
            return result
        if freshness != required:
            result["status"] = "VIOLATED"
            result["reason"] = f"EXPECTED_{required}"
            return result
        if completed_session_age is None:
            result["status"] = "UNMEASURED"
            result["reason"] = "COMPLETED_SESSION_AGE_MISSING"
            return result
        try:
            age = int(completed_session_age)
        except (TypeError, ValueError):
            result["status"] = "UNMEASURED"
            result["reason"] = "COMPLETED_SESSION_AGE_INVALID"
            return result
        if age < 0:
            result["status"] = "UNMEASURED"
            result["reason"] = "COMPLETED_SESSION_AGE_INVALID"
            return result
        result["observed_completed_session_age"] = age
        max_age = int(policy.get("max_completed_session_age", 0))
        if age > max_age:
            result["status"] = "VIOLATED"
            result["reason"] = "COMPLETED_SESSION_TOO_OLD"
            return result
        result["status"] = "MET"
        result["reason"] = "SESSION_COMPLETENESS_VERIFIED"
        return result

    result["reason"] = "UNSUPPORTED_MEASUREMENT"
    return result


def data_class_for_metadata(freshness_policy, explicit=None):
    if explicit:
        return explicit
    return FRESHNESS_POLICY_TO_DATA_CLASS.get(str(freshness_policy or ""), "UNKNOWN")


def policy_manifest():
    return {
        "source_trust_model": {
            "version": SOURCE_TRUST_MODEL_VERSION,
            "tiers": TRUST_TIERS,
            "rule": "Trust describes source authority; derived/cache nodes inherit trust from provenance.",
        },
        "freshness_sla": {
            "version": FRESHNESS_SLA_VERSION,
            "policies": FRESHNESS_POLICIES,
            "rule": "SLA status is separate from data quality and must use the correct measurement mode.",
        },
        "current_capabilities": {
            "realtime_quote": "ON_DEMAND_WITH_DATA_LAG_GUARD",
            "minute_series": "ON_DEMAND_WITH_DATA_LAG_GUARD",
            "company_event_discovery": "ON_DEMAND; DISCOVERY_LAG_UNMEASURED_UNTIL_STABLE_FIRST_SEEN_AT",
            "continuous_watcher": "NOT_IMPLEMENTED",
        },
    }
