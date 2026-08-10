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
    path = WORKFLOW_DIR / "pre-merge-security-gate.yml"
    text = path.read_text(encoding="utf-8")

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

    module_text = Path("scripts/quote_resilience.py").read_text(encoding="utf-8")
    assert "http://qt.gtimg.cn" not in module_text
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

    assert "intraday_fast" in realtime
    assert "LOOKLOOK_EXECUTION_MODE" in realtime
    assert "steps.execution-mode.outputs.mode == 'FULL'" in realtime
    assert '"scripts/performance_fast_path.py"' in realtime
    assert '"scripts/test_intraday_fast.py"' in realtime

    assert "execution_mode:" in reusable
    assert "default: AUTO" in reusable
    assert "LOOKLOOK_EXECUTION_MODE" in reusable
    assert "steps.execution-mode.outputs.mode == 'FULL'" in reusable

    # Protected-branch merge smoke must retain the complete path, while the
    # push selftest gives the latency-sensitive path a real reusable run.
    assert "execution_mode: FULL" in premerge
    assert "Run intraday fast-path behavior tests" in premerge
    assert "execution_mode: INTRADAY_FAST" in selftest
    assert "Run intraday fast-path behavior tests" in selftest
    print("PASS intraday_fast_workflow_contract")


def main():
    tests = [
        test_actions_are_sha_pinned,
        test_reusable_defaults_to_called_workflow_sha,
        test_pre_merge_gate_is_unconditional_for_protected_branches,
        test_tencent_transport_never_downgrades_to_http,
        test_event_pdf_parser_is_version_and_hash_pinned,
        test_event_pdf_redirects_stay_on_official_host,
        test_intraday_fast_workflow_contract,
    ]
    for test in tests:
        test()
    print(f"WORKFLOW_SECURITY_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
