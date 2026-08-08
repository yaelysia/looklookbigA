import io
import json
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

import company_events


MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_PDF_TEXT_CHARS = 300_000
MAX_ENRICH_EVENTS_PER_STOCK = 3
FACT_ENRICH_TYPES = {
    "EARNINGS_FORECAST",
    "EARNINGS_EXPRESS",
    "PERIODIC_REPORT",
    "BUYBACK",
    "HOLDER_INCREASE",
    "HOLDER_DECREASE",
    "UNLOCK",
    "MAJOR_CONTRACT",
    "M&A",
    "DIVIDEND",
    "EQUITY_INCENTIVE",
    "LITIGATION",
    "REGULATORY",
    "TRADING_ANOMALY",
    "SUSPENSION_RESUMPTION",
}

_NUMBER = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
_RANGE = r"\s*(?:～|~|—|–|-|至|到)\s*"


def _download_pdf(url, timeout=10):
    if not str(url or "").startswith("https://static.cninfo.com.cn/"):
        raise ValueError("only official CNINFO static PDF URLs are allowed")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": company_events.HEADERS["User-Agent"],
            "Referer": "https://www.cninfo.com.cn/",
            "Accept": "application/pdf,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        length = resp.headers.get("Content-Length")
        if length:
            try:
                if int(length) > MAX_PDF_BYTES:
                    raise ValueError("announcement PDF exceeds byte limit")
            except ValueError as exc:
                if str(exc) == "announcement PDF exceeds byte limit":
                    raise
        payload = resp.read(MAX_PDF_BYTES + 1)
    if len(payload) > MAX_PDF_BYTES:
        raise ValueError("announcement PDF exceeds byte limit")
    if not payload.startswith(b"%PDF"):
        raise ValueError("announcement document is not a PDF")
    return payload


def _normalize_pdf_text(text):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        raise RuntimeError("PDF parser returned empty text")
    return text[:MAX_PDF_TEXT_CHARS]


def _pypdf_text(pdf_bytes):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf is unavailable") from exc

    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    parts = []
    total = 0
    for page in reader.pages:
        value = page.extract_text() or ""
        if value:
            parts.append(value)
            total += len(value)
        if total >= MAX_PDF_TEXT_CHARS:
            break
    return _normalize_pdf_text("\n".join(parts))


def _pdftotext_text(pdf_bytes):
    tool = shutil.which("pdftotext")
    if not tool:
        raise RuntimeError("pdftotext is unavailable")
    completed = subprocess.run(
        [tool, "-layout", "-", "-"],
        input=pdf_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=12,
        check=False,
    )
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"pdftotext failed: {error[:300]}")
    return _normalize_pdf_text(completed.stdout.decode("utf-8", errors="replace"))


def _pdf_text(pdf_bytes):
    errors = []
    try:
        return _pypdf_text(pdf_bytes), "pypdf"
    except Exception as exc:
        errors.append(f"pypdf={type(exc).__name__}: {exc}")

    try:
        return _pdftotext_text(pdf_bytes), "pdftotext"
    except Exception as exc:
        errors.append(f"pdftotext={type(exc).__name__}: {exc}")

    raise RuntimeError("; ".join(errors))


def _num(text):
    return float(str(text).replace(",", ""))


def _unit_multiplier(unit):
    if unit == "亿元":
        return 100_000_000.0
    if unit == "万元":
        return 10_000.0
    return 1.0


def _earnings_facts(text, facts):
    unit_match = re.search(r"单位\s*[：:]\s*(亿元|万元|元)", text)
    table_unit = unit_match.group(1) if unit_match else None

    profit_pattern = re.compile(
        r"归属于上市公司股东的净利润.{0,80}?" + _NUMBER + _RANGE + _NUMBER
    )
    profit = profit_pattern.search(text)
    if profit and table_unit:
        multiplier = _unit_multiplier(table_unit)
        facts["profit_min_yuan"] = round(_num(profit.group(1)) * multiplier, 2)
        facts["profit_max_yuan"] = round(_num(profit.group(2)) * multiplier, 2)
        facts["profit_table_unit"] = table_unit

    yoy = re.search(
        r"归属于上市公司股东的净利润.{0,180}?比上年同期(?:增长|上升|下降|减少)\s*"
        + _NUMBER + r"\s*%" + _RANGE + _NUMBER + r"\s*%",
        text,
    )
    if not yoy:
        yoy = re.search(
            r"比上年同期(?:增长|上升|下降|减少)\s*" + _NUMBER + r"\s*%" + _RANGE + _NUMBER + r"\s*%",
            text,
        )
    if yoy:
        facts["yoy_min_percent"] = _num(yoy.group(1))
        facts["yoy_max_percent"] = _num(yoy.group(2))

    eps = re.search(
        r"基本每股收益[^0-9]{0,20}" + _NUMBER + _RANGE + _NUMBER,
        text,
    )
    if eps:
        facts["eps_min_yuan"] = _num(eps.group(1))
        facts["eps_max_yuan"] = _num(eps.group(2))

    period = re.search(
        r"业绩预告期间\s*[：:]\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
        r"\s*至\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        text,
    )
    if period:
        facts["period_start_date"] = f"{int(period.group(1)):04d}-{int(period.group(2)):02d}-{int(period.group(3)):02d}"
        facts["period_end_date"] = f"{int(period.group(4)):04d}-{int(period.group(5)):02d}-{int(period.group(6)):02d}"


def _buyback_facts(text, facts):
    ranges = []
    for match in re.finditer(
        r"(?:回购资金|回购金额|资金总额|回购总额).{0,50}?" + _NUMBER + r"\s*(亿元|万元|元).{0,40}?" + _NUMBER + r"\s*(亿元|万元|元)",
        text,
    ):
        first = _num(match.group(1)) * _unit_multiplier(match.group(2))
        second = _num(match.group(3)) * _unit_multiplier(match.group(4))
        ranges.append((min(first, second), max(first, second)))
    if ranges:
        facts["amount_min_yuan"], facts["amount_max_yuan"] = [round(x, 2) for x in ranges[0]]
    cap = re.search(r"不(?:高于|超过)\s*" + _NUMBER + r"\s*元\s*/?\s*股", text)
    if cap:
        facts["price_cap_yuan_per_share"] = _num(cap.group(1))


def _enriched_facts(event, text, parser):
    facts = company_events.extract_facts(
        event.get("event_type"),
        event.get("title"),
        text,
    )
    facts["extraction_scope"] = "ORIGINAL_PDF_TEXT"
    facts["document_extraction"] = {
        "status": "OK",
        "source_url": event.get("source_url"),
        "parser": parser,
    }
    event_type = event.get("event_type")
    if event_type in {"EARNINGS_FORECAST", "EARNINGS_EXPRESS"}:
        _earnings_facts(text, facts)
    elif event_type == "BUYBACK":
        _buyback_facts(text, facts)
    return facts


def enrich_event(event):
    if not isinstance(event, dict):
        return event, False
    if event.get("event_type") not in FACT_ENRICH_TYPES or not event.get("source_url"):
        return event, False
    try:
        text, parser = _pdf_text(_download_pdf(event.get("source_url")))
        event["facts"] = _enriched_facts(event, text, parser)
        return event, True
    except Exception as exc:
        facts = dict(event.get("facts") or {})
        facts["document_extraction"] = {
            "status": "UNAVAILABLE",
            "source_url": event.get("source_url"),
            "error": f"{type(exc).__name__}: {exc}",
        }
        event["facts"] = facts
        return event, False


def _update_cache(code, enriched_by_id):
    cache_path = company_events._event_cache_path(code)
    cache = company_events._read_json(cache_path)
    if not isinstance(cache, dict) or not isinstance(cache.get("events"), list):
        return
    changed = False
    for event in cache["events"]:
        event_id = (event or {}).get("event_id")
        if event_id in enriched_by_id:
            event["facts"] = enriched_by_id[event_id].get("facts")
            changed = True
    if changed:
        company_events._write_json(cache_path, cache)


def finalize_snapshot(snapshot_path):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    enriched_count = 0
    failed_count = 0
    selected_count = 0
    parsers = set()

    for code, item in (data.get("detail_stocks") or {}).items():
        container = item.get("events")
        if not isinstance(container, dict):
            continue
        recent = container.get("recent") if isinstance(container.get("recent"), list) else []
        selected = [
            event for event in recent
            if isinstance(event, dict) and event.get("event_type") in FACT_ENRICH_TYPES and event.get("source_url")
        ][:MAX_ENRICH_EVENTS_PER_STOCK]
        selected_count += len(selected)
        enriched_by_id = {}
        for event in selected:
            event, ok = enrich_event(event)
            enriched_by_id[event.get("event_id")] = event
            document = (event.get("facts") or {}).get("document_extraction") or {}
            if document.get("parser"):
                parsers.add(document.get("parser"))
            if ok:
                enriched_count += 1
            else:
                failed_count += 1

        if enriched_by_id:
            for bucket in ("latest", "recent", "upcoming"):
                value = container.get(bucket)
                values = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
                for event in values:
                    event_id = (event or {}).get("event_id")
                    enriched = enriched_by_id.get(event_id)
                    if enriched:
                        event["facts"] = enriched.get("facts")
            _update_cache(code, enriched_by_id)

    summary = data.setdefault("company_events", {})
    summary["fact_enrichment"] = {
        "status": "OK" if failed_count == 0 else "PARTIAL",
        "selected_event_count": selected_count,
        "enriched_event_count": enriched_count,
        "failed_event_count": failed_count,
        "parsers_used": sorted(parsers),
        "preferred_parser": "pypdf==6.14.2",
        "max_events_per_stock": MAX_ENRICH_EVENTS_PER_STOCK,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "COMPANY_EVENT_FACTS "
        f"status={summary['fact_enrichment']['status']} selected={selected_count} "
        f"enriched={enriched_count} failed={failed_count} parsers={sorted(parsers)}",
        flush=True,
    )
