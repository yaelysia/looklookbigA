import json
import time
import urllib.parse


def install(capital_flow_context):
    if getattr(capital_flow_context, "_margin_query_contract_installed", False):
        return

    def fetch_margin_history(base, code, limit=capital_flow_context.MARGIN_KEEP_RECORDS):
        params = {
            "reportName": capital_flow_context.MARGIN_REPORT,
            "columns": "ALL",
            # RPTA_WEB_RZRQ_GGMX exposes SCODE and DATE. Its historical
            # per-security filter grammar uses a double-quoted SCODE value.
            "filter": f'(SCODE="{code}")',
            "sortColumns": "DATE",
            "sortTypes": "-1",
            "pageNumber": "1",
            "pageSize": str(max(limit, 20)),
            "source": "WEB",
            "client": "WEB",
            "_": str(int(time.time() * 1000)),
        }
        url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
        obj = json.loads(base.http_get(url))
        if not isinstance(obj, dict):
            raise RuntimeError("Eastmoney margin response is not an object")
        if obj.get("success") is False:
            raise RuntimeError(
                "Eastmoney margin provider rejected query: "
                f"code={obj.get('code')} message={obj.get('message')}"
            )
        result = obj.get("result") or {}
        rows = result.get("data") or []
        normalized = [
            capital_flow_context._normalize_margin_row(row)
            for row in rows
            if isinstance(row, dict)
        ]
        normalized = [x for x in normalized if x]
        by_date = {x["trade_date"]: x for x in normalized}
        records = [by_date[key] for key in sorted(by_date, reverse=True)][:limit]
        if not records:
            raise RuntimeError("Eastmoney margin query returned no normalized records")
        return records, url

    capital_flow_context.fetch_margin_history = fetch_margin_history
    capital_flow_context._margin_query_contract_installed = True
