# CNINFO PDF redirect security boundary

Official company-event PDF fact enrichment treats `static.cninfo.com.cn` as a strict provenance boundary.

The downloader enforces the boundary at three points:

1. the initial document URL must use HTTPS and the exact host `static.cninfo.com.cn`;
2. every HTTP redirect target is validated before it is followed;
3. the final effective response URL returned by the HTTP client is validated again before any response body is read.

The following are rejected:

- redirects to any other host;
- HTTPS-to-HTTP downgrade;
- deceptive suffix hosts such as `static.cninfo.com.cn.example.com`;
- URLs containing userinfo;
- non-standard HTTPS ports.

A rejected or unavailable PDF does not remove the underlying CNINFO announcement. The existing event record and title/API-level facts remain available, while `facts.document_extraction.status` becomes `UNAVAILABLE` and the fact-enrichment layer is degraded.

Regression coverage lives in `scripts/test_company_event_facts.py` and the invariant is also checked by `scripts/test_workflow_security.py`.
