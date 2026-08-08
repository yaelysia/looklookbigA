import changes_comparability
import changes_since_previous as changes
import test_changes_since_previous as fixtures


changes_comparability.install(changes)


def test_unknown_market_session_never_compares_cumulative_turnover():
    previous = fixtures._snapshot("2026-08-07 10:15:00", amount=100.0)
    current = fixtures._snapshot("2026-08-07 10:30:00", amount=160.0)
    previous["detail_stocks"]["002558"]["quote"].pop("market_time_cst")

    result = changes.build_changes(previous, current, "snapshots/prev.json")
    turnover = result["stocks"]["002558"]["turnover_change"]
    amount = turnover["amount_1e8"]

    assert turnover["same_market_session"] is False
    assert turnover["market_session_before"] is None
    assert turnover["market_session_after"] == "2026-08-07"
    assert amount["before"] == 100.0
    assert amount["after"] == 160.0
    assert amount["delta"] is None
    assert amount["delta_percent_of_before"] is None
    assert amount["comparable"] is False
    assert turnover["incremental_amount_1e8"] is None
    assert turnover["incremental_amount_per_minute_1e8"] is None
    assert "MARKET_SESSION_UNCONFIRMED" in turnover["quality_flags"]
    assert "MARKET_SESSION_RESET" not in turnover["quality_flags"]
    assert not any(
        reason.get("reason") == "CUMULATIVE_TURNOVER_CHANGED"
        for reason in result["stocks"]["002558"]["significance_reasons"]
    )
    print("PASS unknown_session_turnover_non_comparable")


def _make_peer_failure_snapshot():
    current = fixtures._snapshot(
        "2026-08-07 10:30:00",
        latest=10.0,
        pct=1.0,
        vs_market=0.2,
        vs_group=1.3333,
        group_target=1.0,
        group_peers=(3.0, 2.5, 0.5, -0.5, -1.0),
        group_breadth=-33.33,
    )
    group = current["groups"]["game"]
    group["members"][0]["available"] = False
    group["members"][1]["available"] = False
    group["covered_member_count"] = 3
    group["coverage_percent"] = 60.0
    group["status"] = "PARTIAL"
    group["mean_change_percent"] = -0.3333
    group["median_change_percent"] = -0.5
    group["breadth_score_percent"] = -33.33
    group["target_vs_peer_mean_percent"] = 1.3333
    relative = current["market_environment"]["targets"]["002558"]["relative_strength"]
    relative["vs_group_mean_percent"] = 1.3333
    relative["relative_to_group"] = "OUTPERFORM"
    return current


def test_peer_disappearance_cannot_manufacture_rank_or_relative_strength():
    previous = fixtures._snapshot(
        "2026-08-07 10:15:00",
        latest=10.0,
        pct=1.0,
        vs_market=0.2,
        vs_group=0.1,
        group_target=1.0,
        group_peers=(3.0, 2.5, 0.5, -0.5, -1.0),
        group_breadth=20.0,
    )
    current = _make_peer_failure_snapshot()

    result = changes.build_changes(previous, current, "snapshots/prev.json")
    stock = result["stocks"]["002558"]
    group = result["groups"]["game"]

    peer = stock["relative_strength_change"]["peer_universe"]
    assert peer["peer_universe_comparable"] is False
    assert peer["before"]["configured_peer_codes"] == peer["after"]["configured_peer_codes"]
    assert peer["before"]["available_peer_codes"] != peer["after"]["available_peer_codes"]
    assert "PEER_SET_CHANGED" in peer["quality_flags"]
    assert "PEER_COVERAGE_CHANGED" in peer["quality_flags"]

    vs_group = stock["relative_strength_change"]["vs_group_mean_percent"]
    assert vs_group["before"] == 0.1
    assert vs_group["after"] == 1.3333
    assert vs_group["delta"] is None
    assert vs_group["delta_percent_of_before"] is None
    assert vs_group["comparable"] is False
    relative_state = stock["relative_strength_change"]["relative_to_group"]
    assert relative_state["before"] == "INLINE"
    assert relative_state["after"] == "OUTPERFORM"
    assert relative_state["changed"] is False
    assert relative_state["comparable"] is False
    assert stock["strength_direction"] == "UNCHANGED"
    assert stock["strength_basis"] == "MARKET"
    assert not any(
        reason.get("reason") == "RELATIVE_TO_GROUP_CHANGED"
        for reason in stock["significance_reasons"]
    )

    assert group["peer_universe_comparable"] is False
    assert "PEER_SET_CHANGED" in group["quality_flags"]
    assert "PEER_COVERAGE_CHANGED" in group["quality_flags"]
    assert group["target_rank"]["before"] == 3
    assert group["target_rank"]["after"] == 1
    assert group["target_rank"]["rank_improvement"] is None
    assert group["target_rank"]["comparable"] is False
    for field in (
        "mean_change_percent",
        "median_change_percent",
        "breadth_score_percent",
        "target_vs_peer_mean_percent",
    ):
        assert group["metrics"][field]["comparable"] is False
        assert group["metrics"][field]["delta"] is None
    # Coverage itself remains comparable because it describes the data-quality
    # change that invalidated the peer-relative comparison.
    assert group["metrics"]["coverage_percent"]["comparable"] is True
    assert group["metrics"]["coverage_percent"]["delta"] == -40.0
    fake_reasons = {"GROUP_BREADTH_CHANGED", "TARGET_GROUP_RANK_CHANGED"}
    assert not any(
        reason.get("reason") in fake_reasons
        for reason in group["significance_reasons"]
    )
    print("PASS peer_disappearance_non_comparable")


def test_stable_peer_universe_keeps_peer_relative_deltas_comparable():
    previous = fixtures._snapshot(
        "2026-08-07 10:15:00",
        vs_market=0.0,
        vs_group=0.1,
        group_target=1.0,
        group_peers=(2.0, 1.5, 0.5),
        group_breadth=-20.0,
    )
    current = fixtures._snapshot(
        "2026-08-07 10:30:00",
        vs_market=0.0,
        vs_group=1.0,
        group_target=2.0,
        group_peers=(1.5, 1.0, 0.5),
        group_breadth=40.0,
    )

    result = changes.build_changes(previous, current, "snapshots/prev.json")
    stock = result["stocks"]["002558"]
    group = result["groups"]["game"]
    assert stock["relative_strength_change"]["peer_universe"]["peer_universe_comparable"] is True
    assert stock["relative_strength_change"]["vs_group_mean_percent"]["comparable"] is True
    assert stock["relative_strength_change"]["vs_group_mean_percent"]["delta"] == 0.9
    assert stock["strength_basis"] == "GROUP"
    assert stock["strength_direction"] == "STRONGER"
    assert group["peer_universe_comparable"] is True
    assert group["target_rank"]["comparable"] is True
    assert group["target_rank"]["rank_improvement"] is not None
    assert group["metrics"]["breadth_score_percent"]["comparable"] is True
    print("PASS stable_peer_universe_comparable")


def main():
    tests = [
        test_unknown_market_session_never_compares_cumulative_turnover,
        test_peer_disappearance_cannot_manufacture_rank_or_relative_strength,
        test_stable_peer_universe_keeps_peer_relative_deltas_comparable,
    ]
    for test in tests:
        test()
    print(f"CHANGES_COMPARABILITY_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
