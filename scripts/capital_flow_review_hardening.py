import copy
import json


MIN_AVAILABLE_PEERS = 3
MIN_PEER_COVERAGE = 0.60
MIN_SYNC_ACTIVE_PEERS = 3


def _signature(codes):
    values = sorted({str(code) for code in (codes or []) if code})
    return "|".join(values) if values else None


def _compact_quote(quote):
    if not isinstance(quote, dict):
        return None
    return {
        "latest": quote.get("latest"),
        "amount_raw": quote.get("amount_raw"),
        "market_time_cst": quote.get("market_time_cst"),
    }


def _session_guard_turnover(context, value, code, item, previous):
    turnover = (((value or {}).get("observed") or {}).get("turnover") or {})
    current_date = context._market_date((item or {}).get("quote") or {})
    previous_item = (((previous or {}).get("detail_stocks") or {}).get(code) or {})
    previous_date = context._market_date(previous_item.get("quote") or {})

    if not current_date or not previous_date:
        comparable = False
        reason = "MARKET_SESSION_UNCONFIRMED"
    elif current_date != previous_date:
        comparable = False
        reason = "MARKET_SESSION_RESET"
    elif turnover.get("previous_snapshot_comparable"):
        comparable = True
        reason = "SAME_MARKET_SESSION"
    else:
        comparable = False
        reason = "PREVIOUS_RATE_UNAVAILABLE"

    if not comparable:
        turnover["amount_rate_vs_previous_snapshot"] = None
    turnover["previous_snapshot_comparable"] = comparable
    turnover["previous_snapshot_comparability"] = {
        "comparable": comparable,
        "reason": reason,
        "current_market_date": current_date,
        "previous_market_date": previous_date,
    }
    return value


def _recount_generic_summary(changes):
    if not isinstance(changes, dict):
        return
    items = []
    items.extend((changes.get("stocks") or {}).values())
    items.extend((changes.get("groups") or {}).values())
    for key in ("market", "events"):
        item = changes.get(key)
        if isinstance(item, dict):
            items.append(item)

    counts = {"SIGNIFICANT": 0, "MODERATE": 0, "MINOR": 0}
    for item in items:
        severity = str((item or {}).get("significance") or "NONE").upper()
        if severity in counts:
            counts[severity] += 1
    summary = changes.setdefault("summary", {})
    summary["significant_changes"] = counts["SIGNIFICANT"]
    summary["moderate_changes"] = counts["MODERATE"]
    summary["minor_changes"] = counts["MINOR"]


def _margin_severity(capital_flow_changes, value, margin_advanced):
    rate_ratio = (((value.get("turnover") or {}).get("amount_rate_vs_baseline") or {}).get("delta"))
    severity = capital_flow_changes._severity_for_rate(rate_ratio)
    for key, level in (
        ("pressure", "MODERATE"),
        ("absorption", "MODERATE"),
        ("price_volume_confirmation", "MODERATE"),
        ("vwap_acceptance", "MINOR"),
    ):
        if ((value.get(key) or {}).get("changed")):
            severity = capital_flow_changes._max_severity(severity, level)
    peer_strength = (((value.get("peer_context") or {}).get("relative_capital_strength") or {}))
    delta = capital_flow_changes._as_float(peer_strength.get("delta"))
    if peer_strength.get("comparable") and delta is not None and abs(delta) >= 0.5:
        severity = capital_flow_changes._max_severity(severity, "MODERATE")
    if margin_advanced:
        severity = capital_flow_changes._max_severity(severity, "MINOR")
    return severity


def install(capital_flow_context, capital_flow_changes, history_store):
    if getattr(capital_flow_context, "_review_hardening_installed", False):
        return

    # ------------------------------------------------------------------
    # Compact history: persist only the peer fields needed for the next
    # same-session turnover-rate comparison. Real-time price selection never
    # reads these light-stock history quotes.
    # ------------------------------------------------------------------
    original_compact = history_store._compact_snapshot

    def compact_snapshot(data):
        value = original_compact(data)
        light = {}
        for code, item in (data.get("light_stocks") or {}).items():
            quote = _compact_quote((item or {}).get("quote"))
            if quote:
                light[str(code)] = {"code": str(code), "quote": quote}
        value["light_stocks"] = light
        return value

    history_store._compact_snapshot = compact_snapshot

    # ------------------------------------------------------------------
    # Margin data: financing_balance is a required observation for the balance
    # series. A dated row without it cannot enter the normalized series.
    # ------------------------------------------------------------------
    original_normalize_margin_row = capital_flow_context._normalize_margin_row

    def normalize_margin_row(row):
        value = original_normalize_margin_row(row)
        if not isinstance(value, dict):
            return None
        if capital_flow_context._as_float(value.get("financing_balance")) is None:
            return None
        return value

    capital_flow_context._normalize_margin_row = normalize_margin_row

    def margin_change(records, index):
        if not records or len(records) <= index:
            return None
        current = capital_flow_context._as_float((records[0] or {}).get("financing_balance"))
        previous = capital_flow_context._as_float((records[index] or {}).get("financing_balance"))
        if current is None or previous is None:
            return None
        return capital_flow_context._round(current - previous, 2)

    capital_flow_context._margin_change = margin_change

    # Preserve the most recent successfully observed disclosure session. The
    # provider may transiently return a truncated/older data window; that must
    # not move the durable cache backwards or earn a fresh-session SLA MET.
    original_margin_context = capital_flow_context._margin_context

    def margin_context(base, code, now, execution_mode, daily_context=None):
        cache_path = capital_flow_context._margin_cache_path(code)
        cached_payload = capital_flow_context._load_json(cache_path) or {}
        cached_records = cached_payload.get("records") if isinstance(cached_payload.get("records"), list) else []
        valid_cached = [
            row for row in cached_records
            if isinstance(row, dict)
            and row.get("trade_date")
            and capital_flow_context._as_float(row.get("financing_balance")) is not None
        ]
        if cached_records and valid_cached != cached_records:
            cached_payload = copy.deepcopy(cached_payload)
            cached_payload["records"] = valid_cached
            capital_flow_context._write_json(cache_path, cached_payload)
        cached_date = (valid_cached[0] or {}).get("trade_date") if valid_cached else None

        value = original_margin_context(base, code, now, execution_mode, daily_context)
        provider_date = (value or {}).get("as_of_trade_date")
        fresh_provider_result = execution_mode == "FULL" and (value or {}).get("status") == "OK"

        if fresh_provider_result and cached_date and provider_date and provider_date < cached_date:
            restored = copy.deepcopy(cached_payload)
            restored.setdefault("last_provider_regression", {})
            restored["last_provider_regression"] = {
                "observed_at": now.isoformat(timespec="seconds"),
                "cached_trade_date": cached_date,
                "provider_trade_date": provider_date,
                "reason": "MARGIN_SESSION_REGRESSED",
            }
            capital_flow_context._write_json(cache_path, restored)
            fallback = original_margin_context(base, code, now, "INTRADAY_FAST", daily_context)
            fallback["provider_session_regressed"] = True
            fallback["provider_returned_trade_date"] = provider_date
            fallback["session_guard"] = {
                "status": "REGRESSED",
                "reason": "MARGIN_SESSION_REGRESSED",
                "cached_trade_date": cached_date,
                "provider_trade_date": provider_date,
            }
            fallback["error"] = (
                f"MARGIN_SESSION_REGRESSED provider={provider_date} cached={cached_date}; "
                "newer cache preserved"
            )
            return fallback

        value["provider_session_regressed"] = False
        if fresh_provider_result:
            if not cached_date:
                guard_status = "NO_BASELINE"
            elif provider_date > cached_date:
                guard_status = "ADVANCED"
            else:
                guard_status = "SAME_SESSION_CONFIRMED"
        elif (value or {}).get("status") == "CACHED":
            guard_status = "CACHE_ONLY_UNVERIFIED"
        else:
            guard_status = "UNAVAILABLE"
        value["session_guard"] = {
            "status": guard_status,
            "cached_trade_date": cached_date,
            "provider_trade_date": provider_date if fresh_provider_result else None,
        }
        return value

    capital_flow_context._margin_context = margin_context

    # ------------------------------------------------------------------
    # Peer context: the target is not a peer. Relative/rank/sync outputs require
    # sufficient effective peer coverage and preserve both configured and
    # available-set signatures for change comparability.
    # ------------------------------------------------------------------
    def peer_context(snapshot, previous, target_code):
        current_quotes = capital_flow_context._quote_map(snapshot)
        previous_quotes = capital_flow_context._quote_map(previous or {})
        current_time = capital_flow_context._snapshot_time(snapshot)
        previous_time = capital_flow_context._snapshot_time(previous or {})
        interval_minutes = None
        if current_time and previous_time:
            try:
                interval_minutes = (current_time - previous_time).total_seconds() / 60.0
            except TypeError:
                interval_minutes = None
        if interval_minutes is not None and interval_minutes <= 0:
            interval_minutes = None

        previous_primary = (
            (((previous or {}).get("detail_stocks") or {}).get(target_code) or {})
            .get("capital_flow", {})
            .get("peer_context", {})
            .get("primary")
            or {}
        )
        previous_available_signature = previous_primary.get("available_peer_signature")

        results = []
        for group_id, group in (snapshot.get("groups") or {}).items():
            target = ((group or {}).get("target") or {}).get("code")
            if target != target_code:
                continue

            configured_peers = sorted({
                str(x.get("code"))
                for x in ((group or {}).get("members") or [])
                if isinstance(x, dict) and x.get("code") and str(x.get("code")) != target_code
            })
            requested_peer_count = len(configured_peers)
            universe = sorted(configured_peers + [target_code])

            previous_group = ((previous or {}).get("groups") or {}).get(group_id) or {}
            previous_target = (previous_group.get("target") or {}).get("code")
            previous_peers = sorted({
                str(x.get("code"))
                for x in (previous_group.get("members") or [])
                if isinstance(x, dict) and x.get("code") and str(x.get("code")) != target_code
            })
            previous_universe = sorted(previous_peers + ([previous_target] if previous_target else []))
            configured_same = bool(
                previous_group
                and previous_target == target_code
                and configured_peers == previous_peers
            )

            def rate_entry(code):
                cur = current_quotes.get(code) or {}
                old = previous_quotes.get(code) or {}
                current_amount = capital_flow_context._as_float(cur.get("amount_raw"))
                previous_amount = capital_flow_context._as_float(old.get("amount_raw"))
                same_session = bool(
                    capital_flow_context._market_date(cur)
                    and capital_flow_context._market_date(cur) == capital_flow_context._market_date(old)
                )
                rate = None
                if (
                    same_session
                    and interval_minutes
                    and current_amount is not None
                    and previous_amount is not None
                    and current_amount >= previous_amount
                ):
                    rate = (current_amount - previous_amount) / interval_minutes
                price_delta = (
                    capital_flow_context._pct_change(cur.get("latest"), old.get("latest"))
                    if same_session else None
                )
                return {
                    "code": code,
                    "amount_rate_since_previous": rate,
                    "price_delta_percent": price_delta,
                }

            target_entry = rate_entry(target_code)
            peer_entries = [rate_entry(code) for code in configured_peers]
            available_peers = [x for x in peer_entries if x["amount_rate_since_previous"] is not None]
            available_codes = sorted(x["code"] for x in available_peers)
            available_count = len(available_peers)
            coverage = available_count / requested_peer_count if requested_peer_count else 0.0
            minimum_required = min(MIN_AVAILABLE_PEERS, requested_peer_count) if requested_peer_count else 0
            target_rate = target_entry.get("amount_rate_since_previous")
            sufficient = bool(
                requested_peer_count
                and target_rate is not None
                and available_count >= minimum_required
                and coverage >= MIN_PEER_COVERAGE
            )

            median_rate = (
                capital_flow_context._median([x["amount_rate_since_previous"] for x in available_peers])
                if sufficient else None
            )
            relative = capital_flow_context._ratio(target_rate, median_rate) if sufficient else None
            ranked = []
            rank = None
            if sufficient:
                ranked = sorted(
                    available_peers + [target_entry],
                    key=lambda x: x["amount_rate_since_previous"],
                    reverse=True,
                )
                rank = next((i + 1 for i, x in enumerate(ranked) if x["code"] == target_code), None)

            active_up = active_down = 0
            if sufficient and median_rate is not None:
                for entry in available_peers:
                    if entry["amount_rate_since_previous"] < median_rate:
                        continue
                    delta = entry.get("price_delta_percent")
                    if delta is not None and delta > 0:
                        active_up += 1
                    elif delta is not None and delta < 0:
                        active_down += 1
            active_total = active_up + active_down
            if not sufficient or active_total < MIN_SYNC_ACTIVE_PEERS:
                sync = "UNKNOWN"
                direction = "UNKNOWN"
            else:
                dominant = max(active_up, active_down) / active_total
                sync = "STRONG" if dominant >= 0.75 else "MODERATE" if dominant >= 0.60 else "WEAK"
                direction = "UP" if active_up > active_down else "DOWN" if active_down > active_up else "MIXED"

            available_signature = _signature(available_codes)
            current_configured_signature = _signature(universe)
            previous_configured_signature = _signature(previous_universe)
            comparable_to_previous = bool(
                sufficient
                and configured_same
                and previous_available_signature
                and available_signature == previous_available_signature
            )

            if sufficient:
                status = "OK"
                reason = "SUFFICIENT_PEER_COVERAGE"
            elif requested_peer_count == 0:
                status = "UNAVAILABLE"
                reason = "NO_CONFIGURED_PEERS"
            elif available_count == 0 or target_rate is None:
                status = "UNAVAILABLE"
                reason = "NO_SAME_SESSION_PEER_BASELINE"
            else:
                status = "PARTIAL"
                reason = "INSUFFICIENT_PEER_COVERAGE"

            results.append({
                "status": status,
                "reason": reason,
                "group_id": group_id,
                "peer_universe": universe,
                "peer_universe_signature": current_configured_signature,
                "previous_peer_universe_signature": previous_configured_signature,
                "configured_peer_codes": configured_peers,
                "requested_peer_count": requested_peer_count,
                "available_peer_codes": available_codes,
                "available_peer_signature": available_signature,
                "previous_available_peer_signature": previous_available_signature,
                "available_peer_count": available_count,
                "peer_count": available_count,
                "peer_coverage": capital_flow_context._round(coverage, 4),
                "minimum_required_peer_count": minimum_required,
                "minimum_required_coverage": MIN_PEER_COVERAGE,
                "target_included_in_peer_count": False,
                "comparability": {
                    "comparable_to_previous": comparable_to_previous,
                    "reason": (
                        "SAME_CONFIGURED_AND_AVAILABLE_PEER_UNIVERSE"
                        if comparable_to_previous
                        else "PEER_UNIVERSE_OR_AVAILABLE_SET_NONCOMPARABLE"
                    ),
                    "configured_universe_same": configured_same,
                    "available_set_same": bool(
                        previous_available_signature
                        and available_signature == previous_available_signature
                    ),
                },
                "interval_minutes": capital_flow_context._round(interval_minutes, 3),
                "relative_capital_strength": capital_flow_context._round(relative, 3),
                "target_amount_rate_since_previous": capital_flow_context._round(target_rate, 2),
                "peer_median_amount_rate_since_previous": capital_flow_context._round(median_rate, 2),
                "rank": rank,
                "rank_universe_count": len(ranked) if sufficient else 0,
                "sector_sync": sync,
                "sector_active_direction": direction,
                "active_up_count": active_up,
                "active_down_count": active_down,
                "active_peer_count": active_total,
                "method": "same-session cumulative turnover delta per minute; target excluded from peer coverage and sector-sync counts",
            })

        if not results:
            return {"status": "UNAVAILABLE", "groups": [], "primary": None}
        primary = results[0]
        return {"status": primary.get("status") or "UNAVAILABLE", "groups": results, "primary": primary}

    capital_flow_context._peer_context = peer_context

    original_build_capital_flow = capital_flow_context.build_capital_flow

    def build_capital_flow(code, item, mins, previous, snapshot, base, now, execution_mode):
        value = original_build_capital_flow(code, item, mins, previous, snapshot, base, now, execution_mode)
        return _session_guard_turnover(capital_flow_context, value, code, item, previous)

    capital_flow_context.build_capital_flow = build_capital_flow

    # ------------------------------------------------------------------
    # Changes: a new margin disclosure requires strict forward session
    # progression. Regression is an explicit non-comparable state.
    # ------------------------------------------------------------------
    original_build_change = capital_flow_changes.build_change

    def build_change(before, after):
        value = original_build_change(before, after)
        if not isinstance(before, dict) or not isinstance(after, dict) or value.get("status") != "OK":
            return value

        bmargin = (before.get("official_delayed") or {}).get("margin") or {}
        amargin = (after.get("official_delayed") or {}).get("margin") or {}
        before_date = bmargin.get("as_of_trade_date")
        after_date = amargin.get("as_of_trade_date")
        regressed = bool(
            amargin.get("provider_session_regressed")
            or (before_date and after_date and after_date < before_date)
        )
        advanced = bool(
            not regressed
            and before_date
            and after_date
            and after_date > before_date
        )
        before_balance = capital_flow_changes._as_float(bmargin.get("financing_balance"))
        after_balance = capital_flow_changes._as_float(amargin.get("financing_balance"))
        balances_valid = before_balance is not None and after_balance is not None

        if regressed:
            session_state = "REGRESSED"
            margin_reason = "MARGIN_SESSION_REGRESSED"
        elif advanced:
            session_state = "ADVANCED"
            margin_reason = "NEW_MARGIN_DISCLOSURE"
        elif before_date and after_date and before_date == after_date:
            session_state = "SAME"
            margin_reason = "SAME_MARGIN_SESSION"
        else:
            session_state = "UNCONFIRMED"
            margin_reason = "MARGIN_SESSION_UNCONFIRMED"

        if advanced and balances_valid:
            financing = capital_flow_changes._numeric(before_balance, after_balance, 2)
        else:
            financing = {
                "before": bmargin.get("financing_balance"),
                "after": amargin.get("financing_balance"),
                "delta": None,
                "comparable": False,
            }

        value["margin"] = {
            "before_trade_date": before_date,
            "after_trade_date": after_date,
            "provider_returned_trade_date": amargin.get("provider_returned_trade_date"),
            "session_state": session_state,
            "new_disclosed_session": advanced,
            "financing_balance": financing,
            "reason": margin_reason if (balances_valid or not advanced) else "MARGIN_BALANCE_MISSING",
        }
        reasons = [
            reason for reason in (value.get("reason_codes") or [])
            if reason not in {"NEW_MARGIN_DISCLOSURE", "MARGIN_SESSION_REGRESSED", "MARGIN_BALANCE_MISSING"}
        ]
        if regressed:
            reasons.append("MARGIN_SESSION_REGRESSED")
        elif advanced:
            reasons.append("NEW_MARGIN_DISCLOSURE")
            if not balances_valid:
                reasons.append("MARGIN_BALANCE_MISSING")
        value["reason_codes"] = list(dict.fromkeys(reasons))
        value["significance"] = _margin_severity(capital_flow_changes, value, advanced)
        return value

    capital_flow_changes.build_change = build_change

    original_finalize_changes = capital_flow_changes.finalize_snapshot

    def finalize_changes(snapshot_path):
        original_finalize_changes(snapshot_path)
        path = capital_flow_changes.Path(snapshot_path)
        current = json.loads(path.read_text(encoding="utf-8"))
        changes = current.get("changes_since_previous")
        if isinstance(changes, dict):
            _recount_generic_summary(changes)
            path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            summary = changes.get("summary") or {}
            print(
                "CAPITAL_FLOW_SUMMARY_RECOUNT "
                f"significant={summary.get('significant_changes', 0)} "
                f"moderate={summary.get('moderate_changes', 0)} "
                f"minor={summary.get('minor_changes', 0)}",
                flush=True,
            )

    capital_flow_changes.finalize_snapshot = finalize_changes
    capital_flow_context._review_hardening_installed = True
