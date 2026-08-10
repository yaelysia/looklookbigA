def install(data_policy):
    if getattr(data_policy, "_fundamentals_policy_installed", False):
        return
    data_policy.FRESHNESS_POLICIES["FUNDAMENTALS"] = {
        "measurement": "DISCOVERY_LAG",
        "target_seconds": 24 * 60 * 60,
        "hard_limit_seconds": 72 * 60 * 60,
        "requires_first_seen_at": True,
    }
    data_policy.FRESHNESS_POLICY_TO_DATA_CLASS["FUNDAMENTALS"] = "FUNDAMENTALS"
    data_policy._fundamentals_policy_installed = True
