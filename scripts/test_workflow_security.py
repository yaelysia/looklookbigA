import re
from pathlib import Path

import quote_resilience


WORKFLOW_DIR = Path(".github/workflows")
ACTION_REF_RE = re.compile(r"uses:\s+(actions/[A-Za-z0-9_.-]+)@([^\s#]+)")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


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


def main():
    tests = [
        test_actions_are_sha_pinned,
        test_reusable_defaults_to_called_workflow_sha,
        test_tencent_transport_never_downgrades_to_http,
    ]
    for test in tests:
        test()
    print(f"WORKFLOW_SECURITY_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
