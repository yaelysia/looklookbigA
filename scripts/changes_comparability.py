def _dedupe(values):
    return list(dict.fromkeys(value for value in values if value))


def _invalidate_numeric(change):
    if not isinstance(change, dict):
        return change
    change["delta"] = None
    change["delta_percent_of_before"] = None
    change["comparable"] = False
    return change


def _invalidate_state(change):
    if not isinstance(change, dict):
        return change
    change["changed"] = False
    change["comparable"] = False
    return change


def _as_int(value):
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _target_code(group):
    if not isinstance(group, dict):
        return None
    code = (group.get("target") or {}).get("code")
    return str(code) if code not in (None, "") else None


def _peer_descriptor(group):
    if not isinstance(group, dict):
        return {
            "available": False,
            "target_code": None,
            "requested_member_count": None,
            "active_member_count": None,
            "covered_member_count": None,
            "coverage_percent": None,
            "configured_peer_codes": [],
            "available_peer_codes": [],
            "full_requested_universe_known": False,
        }

    target = _target_code(group)
    members = [member for member in (group.get("members") or []) if isinstance(member, dict)]
    configured = sorted(
        {
            str(member.get("code"))
            for member in members
            if member.get("code") not in (None, "") and str(member.get("code")) != str(target)
        }
    )
    available = sorted(
        {
            str(member.get("code"))
            for member in members
            if member.get("code") not in (None, "")
            and str(member.get("code")) != str(target)
            and member.get("available")
            and _as_float(member.get("change_percent")) is not None
        }
    )

    requested = _as_int(group.get("requested_member_count"))
    active = _as_int(group.get("active_member_count"))
    if active is None:
        active = len(configured)
    covered = _as_int(group.get("covered_member_count"))
    if covered is None:
        covered = len(available)
    coverage = _as_float(group.get("coverage_percent"))

    full_known = (
        requested is not None
        and requested >= 0
        and active == requested
        and len(configured) == requested
    )
    return {
        "available": True,
        "target_code": target,
        "requested_member_count": requested,
        "active_member_count": active,
        "covered_member_count": covered,
        "coverage_percent": coverage,
        "configured_peer_codes": configured,
        "available_peer_codes": available,
        "full_requested_universe_known": full_known,
    }


def _peer_context(before_group, after_group):
    before = _peer_descriptor(before_group)
    after = _peer_descriptor(after_group)
    flags = []

    if not before["available"] or not after["available"]:
        flags.append("PEER_CONTEXT_MISSING")
    if before["target_code"] != after["target_code"]:
        flags.append("PEER_TARGET_CHANGED")
    if before["requested_member_count"] != after["requested_member_count"]:
        flags.append("PEER_REQUESTED_UNIVERSE_CHANGED")
    if not before["full_requested_universe_known"] or not after["full_requested_universe_known"]:
        flags.append("PEER_REQUESTED_UNIVERSE_UNCONFIRMED")
    if before["configured_peer_codes"] != after["configured_peer_codes"]:
        flags.append("PEER_SET_CHANGED")
    if before["available_peer_codes"] != after["available_peer_codes"]:
        flags.append("PEER_SET_CHANGED")
    if (
        before["covered_member_count"] != after["covered_member_count"]
        or before["coverage_percent"] != after["coverage_percent"]
    ):
        flags.append("PEER_COVERAGE_CHANGED")

    comparable = (
        before["available"]
        and after["available"]
        and before["target_code"] is not None
        and before["target_code"] == after["target_code"]
        and before["full_requested_universe_known"]
        and after["full_requested_universe_known"]
        and before["requested_member_count"] == after["requested_member_count"]
        and before["configured_peer_codes"] == after["configured_peer_codes"]
        and before["available_peer_codes"] == after["available_peer_codes"]
    )
    return {
        "peer_universe_comparable": comparable,
        "quality_flags": _dedupe(flags),
        "before": before,
        "after": after,
    }


def _group_for_stock(snapshot, code, relative):
    groups = snapshot.get("groups") or {}
    preferred = (relative or {}).get("group_id")
    if preferred is not None and str(preferred) in groups:
        return str(preferred), groups[str(preferred)]

    matches = []
    for group_id, group in groups.items():
        if _target_code(group) == str(code):
            matches.append((str(group_id), group))
    if len(matches) == 1:
        return matches[0]
    return None, None


def _stock_peer_context(previous, current, code, before_relative, after_relative):
    before_id, before_group = _group_for_stock(previous, code, before_relative)
    after_id, after_group = _group_for_stock(current, code, after_relative)
    context = _peer_context(before_group, after_group)
    context["group_id_before"] = before_id
    context["group_id_after"] = after_id
    if before_id != after_id:
        context["peer_universe_comparable"] = False
        context["quality_flags"] = _dedupe(context["quality_flags"] + ["PEER_GROUP_CHANGED"])
    return context


def _recompute_significance(changes, result):
    significance = "NONE"
    for reason in result.get("significance_reasons") or []:
        significance = changes._max_severity(significance, reason.get("severity") or "NONE")
    result["significance"] = significance


def _harden_stock(changes, original, code, before_item, after_item, previous, current, interval_seconds):
    result = original(code, before_item, after_item, previous, current, interval_seconds)

    before_date = changes._market_date(before_item)
    after_date = changes._market_date(after_item)
    same_session = bool(before_date and after_date and before_date == after_date)
    turnover = result.get("turnover_change") or {}
    amount = turnover.get("amount_1e8") or {}
    flags = list(turnover.get("quality_flags") or [])
    if not same_session:
        _invalidate_numeric(amount)
        turnover["incremental_amount_1e8"] = None
        turnover["incremental_amount_per_minute_1e8"] = None
        if before_date and after_date and before_date != after_date:
            flags.append("MARKET_SESSION_RESET")
        else:
            flags.append("MARKET_SESSION_UNCONFIRMED")
    turnover["same_market_session"] = same_session
    turnover["quality_flags"] = _dedupe(flags)

    before_relative, _ = changes._stock_relative(previous, code)
    after_relative, _ = changes._stock_relative(current, code)
    peer_context = _stock_peer_context(previous, current, code, before_relative, after_relative)
    relative = result.get("relative_strength_change") or {}
    relative["peer_universe"] = peer_context
    result["quality_flags"] = _dedupe((result.get("quality_flags") or []) + peer_context["quality_flags"])

    if not peer_context["peer_universe_comparable"]:
        _invalidate_numeric(relative.get("vs_group_mean_percent"))
        _invalidate_state(relative.get("relative_to_group"))
        result["significance_reasons"] = [
            reason
            for reason in (result.get("significance_reasons") or [])
            if reason.get("reason") != "RELATIVE_TO_GROUP_CHANGED"
        ]

    market_delta = ((relative.get("vs_market_percent") or {}).get("delta"))
    group_delta = ((relative.get("vs_group_mean_percent") or {}).get("delta"))
    if peer_context["peer_universe_comparable"] and group_delta is not None:
        strength_delta = group_delta
        strength_basis = "GROUP"
    elif market_delta is not None:
        strength_delta = market_delta
        strength_basis = "MARKET"
    else:
        strength_delta = None
        strength_basis = "NONE"

    threshold = changes.THRESHOLDS["relative_strength_delta_abs"]["MODERATE"]
    if strength_delta is None:
        result["strength_direction"] = "UNKNOWN"
    elif strength_delta >= threshold:
        result["strength_direction"] = "STRONGER"
    elif strength_delta <= -threshold:
        result["strength_direction"] = "WEAKER"
    else:
        result["strength_direction"] = "UNCHANGED"
    result["strength_basis"] = strength_basis
    _recompute_significance(changes, result)
    return result


def _harden_group(changes, original, group_id, before, after):
    result = original(group_id, before, after)
    peer_context = _peer_context(before, after)
    result["peer_universe_comparable"] = peer_context["peer_universe_comparable"]
    result["peer_universe"] = peer_context
    result["quality_flags"] = _dedupe((result.get("quality_flags") or []) + peer_context["quality_flags"])

    rank = result.get("target_rank") or {}
    rank["comparable"] = bool(peer_context["peer_universe_comparable"])
    if not peer_context["peer_universe_comparable"]:
        for field in (
            "mean_change_percent",
            "median_change_percent",
            "breadth_score_percent",
            "target_vs_peer_mean_percent",
        ):
            _invalidate_numeric((result.get("metrics") or {}).get(field))
        rank["rank_improvement"] = None
        result["significance_reasons"] = [
            reason
            for reason in (result.get("significance_reasons") or [])
            if reason.get("reason") not in {"GROUP_BREADTH_CHANGED", "TARGET_GROUP_RANK_CHANGED"}
        ]
    _recompute_significance(changes, result)
    return result


def install(changes):
    if getattr(changes, "_comparability_hardening_installed", False):
        return

    original_stock = changes._stock_change
    original_group = changes._group_change

    def hardened_stock(code, before_item, after_item, previous, current, interval_seconds):
        return _harden_stock(
            changes,
            original_stock,
            code,
            before_item,
            after_item,
            previous,
            current,
            interval_seconds,
        )

    def hardened_group(group_id, before, after):
        return _harden_group(changes, original_group, group_id, before, after)

    changes._stock_change = hardened_stock
    changes._group_change = hardened_group
    changes._comparability_hardening_installed = True
