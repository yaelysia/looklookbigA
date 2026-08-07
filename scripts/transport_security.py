import urllib.request


def install_quote_resilience(quote_resilience):
    """Force the active Tencent quote transport to HTTPS-only.

    This intentionally replaces the legacy helper before quote_resilience.install()
    exposes any fetch path to the runner. A failed HTTPS request raises; it never
    retries the same provider over plaintext HTTP.
    """

    def https_only_tencent_text(tcodes):
        joined = ",".join(tcodes)
        url = "https://qt.gtimg.cn/q=" + joined
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://gu.qq.com/",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        with urllib.request.urlopen(req, timeout=7) as resp:
            return resp.read().decode("gbk", errors="replace")

    quote_resilience._fetch_tencent_text = https_only_tencent_text
    quote_resilience.TRANSPORT_POLICY = {
        "https_only": True,
        "plaintext_http_fallback": False,
    }
