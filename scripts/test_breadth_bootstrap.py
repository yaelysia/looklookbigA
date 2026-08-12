import json
import os
import tempfile
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import breadth_bootstrap
import market_calendar
import market_environment


def _now(text):
    return datetime.fromisoformat(text).replace(tzinfo=market_calendar.CST)


def _breadth(now):
    return {
        "status": "OK",
        "source": "Eastmoney clist full-universe",
        "collected_at_cst": now.strftime("%Y-%m-%d %H:%M:%S"),
        "market_session_date": now.date().isoformat(),
        "freshness": "LIVE",
        "reported_total_count": 5200,
        "covered_count": 5200,
        "overall": {"up_count": 3000, "down_count": 2000, "flat_count": 200},
    }


def _with_history_root(test):
    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get("MARKET_HISTORY_DIR")
        os.environ["MARKET_HISTORY_DIR"] = tmp
        try:
            test()
        finally:
            if previous is None:
                os.environ.pop("MARKET_HISTORY_DIR", None)
            else:
                os.environ["MARKET_HISTORY_DIR"] = previous


def test_once_per_session_segment_and_new_session():
    def run():
        calls = []

        def source(_base, now, _indices):
            calls.append(now)
            return _breadth(now)

        morning = _now("2026-08-10 09:25:00")
        first = breadth_bootstrap.fetch_market_breadth(None, morning, {}, source)
        second = breadth_bootstrap.fetch_market_breadth(
            None, morning + timedelta(minutes=1), {}, source
        )
        assert len(calls) == 1
        assert first["bootstrap_state"] == "READY"
        assert first["session_segment"] == "MORNING"
        assert first["fast_path"]["access"] == "OWNER_FETCH"
        assert first["cache_age_seconds"] == 0
        assert first["age_seconds"] == 0
        assert first["source_session"] == "2026-08-10"
        assert first["fetched_at"] == "2026-08-10 09:25:00"
        assert first["freshness_status"] == "LIVE"
        assert second["fast_path"]["access"] == "CACHE_HIT"
        assert second["cache_age_seconds"] == 60
        environment = market_environment.build_market_environment(
            {"indices": {}, "groups": {}, "detail_stocks": {}}, second
        )
        quality = environment["data_quality"]
        assert quality["breadth_bootstrap_state"] == "READY"
        assert quality["breadth_bootstrap_key"] == "CN_A:2026-08-10:MORNING"
        assert quality["breadth_session_segment"] == "MORNING"
        assert quality["breadth_bootstrap_revision"] is not None
        assert quality["breadth_cache_age_seconds"] == 60
        assert quality["breadth_source_session"] == "2026-08-10"
        assert quality["breadth_fetched_at"] == "2026-08-10 09:25:00"
        assert quality["breadth_freshness_status"] == "LIVE"
        assert second["provenance"]["session_binding"] == quality["breadth_bootstrap_key"]

        afternoon = _now("2026-08-10 12:55:00")
        afternoon_first = breadth_bootstrap.fetch_market_breadth(None, afternoon, {}, source)
        breadth_bootstrap.fetch_market_breadth(
            None, afternoon + timedelta(minutes=1), {}, source
        )
        assert len(calls) == 2
        assert afternoon_first["session_segment"] == "AFTERNOON"

        next_session = _now("2026-08-11 09:25:00")
        next_first = breadth_bootstrap.fetch_market_breadth(None, next_session, {}, source)
        assert len(calls) == 3
        assert next_first["bootstrap_key"] == "CN_A:2026-08-11:MORNING"

    _with_history_root(run)


def test_concurrent_call_has_one_owner_and_pending_reader():
    def run():
        entered = threading.Event()
        release = threading.Event()
        calls = {"count": 0}
        owner_result = {}
        now = _now("2026-08-12 09:25:00")

        def source(_base, value, _indices):
            calls["count"] += 1
            entered.set()
            assert release.wait(timeout=2)
            return _breadth(value)

        def owner():
            owner_result["value"] = breadth_bootstrap.fetch_market_breadth(None, now, {}, source)

        thread = threading.Thread(target=owner)
        thread.start()
        assert entered.wait(timeout=2)
        pending = breadth_bootstrap.fetch_market_breadth(None, now, {}, source)
        assert pending["bootstrap_state"] == "PENDING"
        assert calls["count"] == 1
        release.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert owner_result["value"]["bootstrap_state"] == "READY"
        cached = breadth_bootstrap.fetch_market_breadth(None, now, {}, source)
        assert cached["bootstrap_state"] == "READY"
        assert calls["count"] == 1

    _with_history_root(run)


def test_failure_retry_and_expired_owner_recovery():
    def run():
        calls = {"count": 0}
        now = _now("2026-08-13 09:25:00")

        def source(_base, value, _indices):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("synthetic failure")
            return _breadth(value)

        failed = breadth_bootstrap.fetch_market_breadth(None, now, {}, source)
        waiting = breadth_bootstrap.fetch_market_breadth(
            None, now + timedelta(seconds=30), {}, source
        )
        retried = breadth_bootstrap.fetch_market_breadth(
            None, now + timedelta(seconds=61), {}, source
        )
        assert failed["bootstrap_state"] == "FAILED"
        assert waiting["bootstrap_state"] == "FAILED"
        assert retried["bootstrap_state"] == "READY"
        assert calls["count"] == 2

        afternoon = _now("2026-08-13 12:55:00")
        identity = breadth_bootstrap.session_identity(afternoon)
        path = breadth_bootstrap._state_path(identity)
        expired = {
            "schema_version": 1,
            **identity,
            "bootstrap_state": "PENDING",
            "owner": "crashed-owner",
            "attempts": 1,
            "lease_expires_at": (afternoon - timedelta(seconds=1)).isoformat(),
            "bootstrap_revision": {"run_id": 1},
        }
        breadth_bootstrap._write_json(path, expired)
        recovered = breadth_bootstrap.fetch_market_breadth(None, afternoon, {}, source)
        assert recovered["bootstrap_state"] == "READY"
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["attempts"] == 2
        assert stored["owner"] is None

    _with_history_root(run)


def test_stale_cache_is_explicit_and_never_silently_refetched():
    def run():
        calls = {"count": 0}

        def source(_base, value, _indices):
            calls["count"] += 1
            return _breadth(value)

        first_at = _now("2026-08-14 09:25:00")
        breadth_bootstrap.fetch_market_breadth(None, first_at, {}, source)
        stale = breadth_bootstrap.fetch_market_breadth(
            None, first_at + timedelta(minutes=20), {}, source
        )
        assert calls["count"] == 1
        assert stale["bootstrap_state"] == "STALE"
        assert stale["freshness"] == "STALE"
        assert stale["status"] == "PARTIAL"
        assert stale["cache_age_seconds"] == 1200
        assert stale["age_seconds"] == 1200
        assert stale["freshness_status"] == "STALE"
        assert stale["fast_path"]["network_refresh"] == "NOT_REQUIRED"

    _with_history_root(run)


def test_non_required_and_unverified_calendar_states_do_not_fetch():
    calls = {"count": 0}

    def source(_base, value, _indices):
        calls["count"] += 1
        return _breadth(value)

    closed = breadth_bootstrap.fetch_market_breadth(
        None, _now("2026-08-10 18:00:00"), {}, source
    )
    unverified = breadth_bootstrap.fetch_market_breadth(
        None, _now("2027-01-04 09:25:00"), {}, source
    )
    assert closed["bootstrap_state"] == "NOT_REQUIRED"
    assert unverified["bootstrap_state"] == "UNVERIFIED"
    assert calls["count"] == 0


def test_source_session_mismatch_fails_without_publishing_ready():
    def run():
        calls = {"count": 0}
        now = _now("2026-08-17 09:25:00")

        def source(_base, value, _indices):
            calls["count"] += 1
            result = _breadth(value)
            result["market_session_date"] = "2026-08-14"
            return result

        failed = breadth_bootstrap.fetch_market_breadth(None, now, {}, source)
        identity = breadth_bootstrap.session_identity(now)
        stored = json.loads(breadth_bootstrap._state_path(identity).read_text(encoding="utf-8"))
        assert failed["bootstrap_state"] == "FAILED"
        assert failed["source_session"] is None
        assert "breadth session mismatch" in failed["error"]
        assert stored["bootstrap_state"] == "FAILED"
        assert stored["breadth"] is None
        assert calls["count"] == 1

    _with_history_root(run)


def test_market_environment_install_preserves_non_ready_bootstrap_state():
    now = _now("2026-08-10 09:25:00")
    identity = breadth_bootstrap.session_identity(now)
    pending = breadth_bootstrap._base_response(
        identity, now, "PENDING", "BOOTSTRAP_OWNER_ACTIVE"
    )
    original_fetch = market_environment.fetch_market_breadth
    original_last = market_environment.LAST_BREADTH
    fake_base = SimpleNamespace(fetch_indices=lambda _now: {})
    try:
        market_environment.fetch_market_breadth = lambda _base, _now, _indices: pending
        market_environment.install(fake_base)
        fake_base.fetch_indices(now)
        assert market_environment.LAST_BREADTH["bootstrap_state"] == "PENDING"
        assert market_environment.LAST_BREADTH["error"] == "BOOTSTRAP_OWNER_ACTIVE"
    finally:
        market_environment.fetch_market_breadth = original_fetch
        market_environment.LAST_BREADTH = original_last


def main():
    tests = [
        test_once_per_session_segment_and_new_session,
        test_concurrent_call_has_one_owner_and_pending_reader,
        test_failure_retry_and_expired_owner_recovery,
        test_stale_cache_is_explicit_and_never_silently_refetched,
        test_non_required_and_unverified_calendar_states_do_not_fetch,
        test_source_session_mismatch_fails_without_publishing_ready,
        test_market_environment_install_preserves_non_ready_bootstrap_state,
    ]
    for test in tests:
        test()
    print(f"BREADTH_BOOTSTRAP_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
