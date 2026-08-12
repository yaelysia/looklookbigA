import re
from pathlib import Path

import quote_resilience


WORKFLOW_DIR = Path(".github/workflows")
ACTION_REF_RE = re.compile(r"uses:\s+(actions/[A-Za-z0-9_.-]+)@([^\s#]+)")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PYPDF_REQUIREMENT = "pypdf==6.14.2 --hash=sha256:3f07891af76dc002657e04993ab9b4de81de29f9013b9761d0b7968bff12e946"


def test_actions_are_sha_pinned():
    failures = []
    found = 0
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        for action, ref in ACTION_REF_RE.findall(text):
            found += 1
            if not FULL_SHA_RE.fullmatch(ref):
                failures.append(f"{path}: {action}@{ref}")
    assert found > 0, "no GitHub actions references were found"
    assert not failures, "mutable GitHub Action refs found: " + "; ".join(failures)
    print(f"PASS actions_sha_pinned refs={found}")


def test_reusable_defaults_to_called_workflow_sha():
    text = (WORKFLOW_DIR / "reusable-a-share-quotes.yml").read_text(encoding="utf-8")
    assert "default: \"\"" in text
    assert "WORKFLOW_SHA: ${{ job.workflow_sha }}" in text
    assert "source_ref must be an immutable 40-character commit SHA" in text
    assert "ENGINE_REVISION_VERIFIED" in text
    assert "default: master" not in text
    print("PASS reusable_engine_binds_to_job_workflow_sha")


def test_pre_merge_gate_is_unconditional_for_protected_branches():
    text = (WORKFLOW_DIR / "pre-merge-security-gate.yml").read_text(encoding="utf-8")
    assert "pull_request:" in text
    assert "- master" in text
    assert "- v1" in text
    assert "paths:" not in text
    assert "pre-merge-security-gate:" in text
    assert "name: pre-merge-security-gate" in text
    assert "needs:" in text
    assert "- safety-tests" in text
    assert "- reusable-smoke" in text
    assert 'if: ${{ always() }}' in text
    assert "enable_history_cache: false" in text
    assert "Run event fact continuity tests" in text
    assert "Run history read-after_write continuity tests" not in text
    assert "Run history read-after-write continuity tests" in text
    assert "Run exact-attempt history artifact tests" in text
    assert "Run authoritative A-share calendar tests" in text
    assert "Run complete minute-history tests" in text
    assert "Run synchronized relative-strength window tests" in text
    assert "Run FAST breadth bootstrap state-machine tests" in text
    print("PASS unconditional_pre_merge_security_gate")


def test_tencent_transport_never_downgrades_to_http():
    seen = []
    original = quote_resilience.urllib.request.urlopen

    def fail_https(request, timeout=None):
        url = request.full_url
        seen.append(url)
        raise RuntimeError("synthetic HTTPS failure")

    try:
        quote_resilience.urllib.request.urlopen = fail_https
        try:
            quote_resilience._fetch_tencent_text(["sz002558"])
        except RuntimeError as exc:
            assert "synthetic HTTPS failure" in str(exc)
        else:
            raise AssertionError("synthetic HTTPS failure should propagate")
    finally:
        quote_resilience.urllib.request.urlopen = original

    assert seen == ["https://qt.gtimg.cn/q=sz002558"], seen
    assert all(url.startswith("https://") for url in seen)
    assert quote_resilience.TRANSPORT_POLICY["https_only"] is True
    assert quote_resilience.TRANSPORT_POLICY["plaintext_http_fallback"] is False
    assert "http://qt.gtimg.cn" not in Path("scripts/quote_resilience.py").read_text(encoding="utf-8")
    print("PASS tencent_https_failure_has_no_http_fallback")


def test_event_pdf_parser_is_version_and_hash_pinned():
    requirement = Path("requirements-event-facts.txt").read_text(encoding="utf-8").strip()
    assert requirement == PYPDF_REQUIREMENT
    realtime = (WORKFLOW_DIR / "realtime-quotes.yml").read_text(encoding="utf-8")
    reusable = (WORKFLOW_DIR / "reusable-a-share-quotes.yml").read_text(encoding="utf-8")
    for name, text in (("realtime", realtime), ("reusable", reusable)):
        assert "--require-hashes" in text, name
        assert "--no-deps" in text, name
        assert "requirements-event-facts.txt" in text, name
        assert "pip install pypdf" not in text, name
    print("PASS event_pdf_parser_hash_pinned")


def test_event_pdf_redirects_stay_on_official_host():
    text = Path("scripts/company_event_facts.py").read_text(encoding="utf-8")
    assert 'CNINFO_PDF_HOST = "static.cninfo.com.cn"' in text
    assert "class _CninfoPdfRedirectHandler" in text
    assert '_validate_cninfo_pdf_url(newurl, stage="redirect")' in text
    assert "build_opener(_CninfoPdfRedirectHandler())" in text
    assert '_validate_cninfo_pdf_url(resp.geturl(), stage="final")' in text
    print("PASS event_pdf_redirect_provenance_boundary")


def test_intraday_fast_workflow_contract():
    realtime = (WORKFLOW_DIR / "realtime-quotes.yml").read_text(encoding="utf-8")
    reusable = (WORKFLOW_DIR / "reusable-a-share-quotes.yml").read_text(encoding="utf-8")
    premerge = (WORKFLOW_DIR / "pre-merge-security-gate.yml").read_text(encoding="utf-8")
    selftest = (WORKFLOW_DIR / "reusable-selftest.yml").read_text(encoding="utf-8")
    runner = Path("scripts/realtime_quotes_watchlist_runner.py").read_text(encoding="utf-8")

    assert "intraday_fast" in realtime
    assert "LOOKLOOK_EXECUTION_MODE" in realtime
    assert "steps.execution-mode.outputs.mode == 'FULL'" in realtime
    assert '"scripts/performance_fast_path.py"' in realtime
    assert '"scripts/intraday_fast_tail.py"' in realtime
    assert '"scripts/event_fact_continuity.py"' in realtime
    assert '"scripts/history_continuity.py"' in realtime
    assert '"scripts/history_artifact.py"' in realtime
    assert '"scripts/market_calendar.py"' in realtime
    assert '"scripts/minute_history.py"' in realtime
    assert '"scripts/relative_strength_windows.py"' in realtime
    assert '"scripts/breadth_bootstrap.py"' in realtime
    assert '"scripts/resolve_previous_realtime_run.py"' in realtime
    assert '"scripts/test_intraday_fast.py"' in realtime
    assert '"scripts/test_relative_strength_windows.py"' in realtime
    assert '"scripts/test_breadth_bootstrap.py"' in realtime
    assert "persist-history:" not in realtime

    assert "event_fact_continuity.install(company_events, company_event_facts)" in runner
    assert "history_continuity.install_manifest_revision(history_store)" in runner
    assert runner.index(
        '"minute_history", minute_history.finalize_snapshot'
    ) < runner.index('"intraday_metrics", intraday_metrics.finalize_snapshot')

    assert "execution_mode:" in reusable
    assert "default: AUTO" in reusable
    assert "LOOKLOOK_EXECUTION_MODE" in reusable
    assert "steps.execution-mode.outputs.mode == 'FULL'" in reusable
    assert "group: looklookbiga-reusable-${{ github.repository_id }}-${{ inputs.cache_namespace }}" in reusable
    assert "cancel-in-progress: false" in reusable

    assert "execution_mode: FULL" in premerge
    assert "Run intraday fast-path behavior tests" in premerge
    assert "Run event fact continuity tests" in premerge
    assert "Run history read-after-write continuity tests" in premerge
    assert "Run exact-attempt history artifact tests" in premerge
    assert "Run authoritative A-share calendar tests" in premerge
    assert "Run complete minute-history tests" in premerge
    assert "Run synchronized relative-strength window tests" in premerge
    assert "Run FAST breadth bootstrap state-machine tests" in premerge
    assert "execution_mode: INTRADAY_FAST" in selftest
    assert '"scripts/intraday_fast_tail.py"' in selftest
    assert '"scripts/relative_strength_windows.py"' in selftest
    assert '"scripts/breadth_bootstrap.py"' in selftest
    assert "Run intraday fast-path behavior tests" in selftest
    assert "Run synchronized relative-strength window tests" in selftest
    assert "Run FAST breadth bootstrap state-machine tests" in selftest
    print("PASS intraday_fast_workflow_contract")


def test_realtime_history_read_after_write_barrier():
    text = (WORKFLOW_DIR / "realtime-quotes.yml").read_text(encoding="utf-8")
    resolver = Path("scripts/resolve_previous_realtime_run.py").read_text(encoding="utf-8")

    assert "actions: read" in text
    assert "group: realtime-a-share-${{ github.ref }}" in text
    assert "cancel-in-progress: false" in text
    assert "Resolve exact previous successful master run" in text
    assert "python3 scripts/resolve_previous_realtime_run.py" in text
    assert "Restore exact previous successful history artifact" in text
    assert "python3 scripts/history_artifact.py" in text
    assert "--current .market-data/history" in text
    assert "--run-id '${{ steps.previous-realtime.outputs.run_id }}'" in text
    assert "--run-attempt '${{ steps.previous-realtime.outputs.run_attempt }}'" in text
    assert "--head-sha '${{ steps.previous-realtime.outputs.head_sha }}'" in text
    assert "continue-on-error: true" in text
    assert "Verify fallback baseline is not behind previous success" in text
    assert "python3 scripts/history_continuity.py verify" in text
    assert "--expected-started-at" in text
    assert "branch=master&status=success" in resolver
    assert 'API_ROOT = "https://api.github.com"' in resolver
    print("PASS realtime_exact_previous_run_read_barrier")


def test_realtime_rerun_artifact_uploads_are_overwrite_safe():
    realtime = (WORKFLOW_DIR / "realtime-quotes.yml").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    realtime_block = realtime.split("- name: Upload realtime snapshot", 1)[1].split(
        "- name: Upload market history state", 1
    )[0]
    history_block = realtime.split("- name: Upload market history state", 1)[1]
    assert "name: realtime-snapshot" in realtime_block
    assert "overwrite: true" in realtime_block
    assert "name: market-history-state" in history_block
    assert "overwrite: true" in history_block
    assert "artifact ID 不在 rerun 前基线集合中" in readme
    assert "overwrite: true" in readme
    print("PASS realtime_rerun_artifact_overwrite_contract")


def test_background_history_persistence_is_master_only_and_monotonic():
    text = (WORKFLOW_DIR / "persist-market-history.yml").read_text(encoding="utf-8")
    assert "workflow_run:" in text
    assert "- Realtime A-share Quotes" in text
    assert "branches:" in text and "- master" in text
    assert "- completed" in text
    assert "github.event.workflow_run.conclusion == 'success'" in text
    assert "python3 .engine/scripts/history_artifact.py" in text
    assert "--run-id '${{ github.event.workflow_run.id }}'" in text
    assert "--run-attempt '${{ github.event.workflow_run.run_attempt }}'" in text
    assert "--head-sha '${{ github.event.workflow_run.head_sha }}'" in text
    assert "actions: read" in text
    assert "contents: write" in text
    assert "cancel-in-progress: false" in text

    # The incoming artifact is staged separately. It cannot overwrite history
    # before the exact triggering engine revision applies the monotonic guard.
    assert "--current .incoming-history" in text
    assert "path: history" not in text
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in text
    assert "path: .engine" in text
    assert "python3 .engine/scripts/history_continuity.py persist" in text
    assert "--current history" in text
    assert "--incoming .incoming-history" in text
    assert "--expected-run-id '${{ github.event.workflow_run.id }}'" in text
    assert "--expected-run-attempt '${{ github.event.workflow_run.run_attempt }}'" in text
    assert "--expected-head-sha '${{ github.event.workflow_run.head_sha }}'" in text
    print("PASS background_history_persistence_master_only_monotonic")


def main():
    tests = [
        test_actions_are_sha_pinned,
        test_reusable_defaults_to_called_workflow_sha,
        test_pre_merge_gate_is_unconditional_for_protected_branches,
        test_tencent_transport_never_downgrades_to_http,
        test_event_pdf_parser_is_version_and_hash_pinned,
        test_event_pdf_redirects_stay_on_official_host,
        test_intraday_fast_workflow_contract,
        test_realtime_history_read_after_write_barrier,
        test_realtime_rerun_artifact_uploads_are_overwrite_safe,
        test_background_history_persistence_is_master_only_and_monotonic,
    ]
    for test in tests:
        test()
    print(f"WORKFLOW_SECURITY_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
