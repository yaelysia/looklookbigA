import hashlib
import html
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


CST = timezone(timedelta(hours=8))
CNINFO_STOCK_MAP_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_BASE = "https://static.cninfo.com.cn/"
DEFAULT_LOOKBACK_DAYS = 30
ALLOWED_LOOKBACK_DAYS = {7, 30, 90}
OVERLAP_REFRESH_DAYS = 7
MAP_CACHE_TTL_DAYS = 7
PAGE_SIZE = 30
MAX_PAGES = 6
MAX_CACHED_EVENTS = 300
EVENT_CACHE_SCHEMA = 1
MAP_CACHE_SCHEMA = 1

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
    "Referer": "https://www.cninfo.com.cn/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/plain, */*",
}

EVENT_RULES = [
    ("EARNINGS_FORECAST", ("业绩预告",)),
    ("EARNINGS_EXPRESS", ("业绩快报",)),
    ("PERIODIC_REPORT", ("年度报告", "年报", "半年度报告", "半年报", "季度报告", "一季度报告", "三季度报告")),
    ("BUYBACK", ("回购",)),
    ("HOLDER_INCREASE", ("增持",)),
    ("HOLDER_DECREASE", ("减持",)),
    ("UNLOCK", ("解除限售", "解禁", "限售股份上市流通", "限售股上市流通")),
    ("PLEDGE", ("股份质押", "股票质押", "解除质押", "质押股份")),
    ("CONVERTIBLE_BOND", ("可转换公司债券", "可转债",)),
    ("PREFERRED_SHARES", ("优先股",)),
    ("MAJOR_CONTRACT", ("重大合同", "中标", "项目定点", "签订合同", "合同进展")),
    ("M&A", ("重大资产重组", "资产重组", "发行股份购买资产", "并购", "收购")),
    ("REFINANCING", ("向特定对象发行", "定向增发", "非公开发行", "配股",)),
    ("DIVIDEND", ("利润分配", "权益分派", "分红", "除权", "除息")),
    ("EQUITY_INCENTIVE", ("股权激励", "限制性股票激励", "股票期权激励")),
    ("LITIGATION", ("诉讼", "仲裁")),
    ("REGULATORY", ("问询函", "监管", "处罚", "立案", "风险提示", "纪律处分", "警示函")),
    ("TRADING_ANOMALY", ("交易异常波动", "异常波动")),
    ("SUSPENSION_RESUMPTION", ("停牌", "复牌")),
    ("INVESTOR_RELATIONS", ("投资者关系活动", "机构调研", "调研活动", "调研")),
]

HIGH_IMPORTANCE_TYPES = {
    "EARNINGS_FORECAST",
    "EARNINGS_EXPRESS",
    "BUYBACK",
    "MAJOR_CONTRACT",
    "M&A",
    "LITIGATION",
    "REGULATORY",
    "TRADING_ANOMALY",
    "SUSPENSION_RESUMPTION",
}
MEDIUM_IMPORTANCE_TYPES = {
    "PERIODIC_REPORT",
    "HOLDER_INCREASE",
    "HOLDER_DECREASE",
    "UNLOCK",
    "DIVIDEND",
    "EQUITY_INCENTIVE",
    "PLEDGE",
    "CONVERTIBLE_BOND",
    "PREFERRED_SHARES",
    "REFINANCING",
}

_CORRECTION_RE = re.compile(r"更正|补充|修订|修正")
_PROGRESS_RE = re.compile(r"进展|实施进展|实施情况|完成")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_DATE_CN_RE = re.compile(r"(?<!\d)(20\d{2})年(\d{1,2})月(\d{1,2})日")
_DATE_ISO_RE = re.compile(r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_AMOUNT_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(亿元|万元|元)(?![\d股])")
_PERCENT_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*%")
_PRICE_CAP_RE = re.compile(r"不(?:高于|超过)\s*(\d+(?:\.\d+)?)\s*元\s*/?\s*股")
_YOY_RANGE_RE = re.compile(r"(?:增长|上升|下降|减少|变动)[^\d%]{0,12}(\d+(?:\.\d+)?)\s*%[^\d%]{0,12}(?:至|到|—|-)[^\d%]{0,8}(\d+(?:\.\d+)?)\s*%")


def _history_root():
    return Path(os.environ.get("MARKET_HISTORY_DIR", ".market-data/history"))


def _events_root():
    return _history_root() / "events"


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _clean_title(value):
    text = html.unescape(str(value or ""))
    text = _HTML_TAG_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _request_json(url, method="GET", form=None, timeout=10, attempts=2):
    if not url.startswith("https://"):
        raise ValueError("company event transport requires HTTPS")
    data = None
    headers = dict(HEADERS)
    if method == "POST":
        data = urllib.parse.urlencode(form or {}).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["Origin"] = "https://www.cninfo.com.cn"
    last_error = None
    for attempt in range(max(1, attempts)):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
                return json.loads(payload)
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.35 * (attempt + 1))
    raise last_error or RuntimeError("CNINFO request failed")


def _parse_time(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10**12:
            numeric /= 1000.0
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc).astimezone(CST)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        return _parse_time(int(text))
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=CST)
        except ValueError:
            continue
    return None


def _iso(dt):
    return dt.astimezone(CST).isoformat(timespec="seconds") if dt else None


def _event_cache_path(code):
    return _events_root() / f"{code}.json"


def _map_cache_path():
    return _events_root() / "_cninfo_stock_map.json"


def _map_from_payload(payload):
    rows = (payload or {}).get("stockList") or []
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").zfill(6)
        org_id = row.get("orgId")
        if len(code) == 6 and code.isdigit() and org_id:
            out[code] = {
                "org_id": str(org_id),
                "name": row.get("zwjc") or row.get("name"),
                "category": row.get("category"),
            }
    return out


def _load_stock_map(now):
    path = _map_cache_path()
    cached = _read_json(path) or {}
    cached_map = cached.get("stocks") if isinstance(cached.get("stocks"), dict) else {}
    updated = _parse_time(cached.get("updated_at_cst"))
    if cached_map and updated and (now - updated) <= timedelta(days=MAP_CACHE_TTL_DAYS):
        return cached_map, {
            "state": "HIT",
            "source": "history/events/_cninfo_stock_map.json",
            "updated_at_cst": cached.get("updated_at_cst"),
            "error": None,
        }

    try:
        payload = _request_json(CNINFO_STOCK_MAP_URL, timeout=10, attempts=2)
        fresh_map = _map_from_payload(payload)
        if not fresh_map:
            raise RuntimeError("CNINFO stock map returned no usable stock entries")
        cache_payload = {
            "schema_version": MAP_CACHE_SCHEMA,
            "source": "CNINFO",
            "source_url": CNINFO_STOCK_MAP_URL,
            "updated_at_cst": _iso(now),
            "stocks": fresh_map,
        }
        _write_json(path, cache_payload)
        return fresh_map, {
            "state": "REFRESHED",
            "source": "CNINFO",
            "updated_at_cst": _iso(now),
            "error": None,
        }
    except Exception as exc:
        if cached_map:
            return cached_map, {
                "state": "STALE_FALLBACK",
                "source": "history/events/_cninfo_stock_map.json",
                "updated_at_cst": cached.get("updated_at_cst"),
                "error": f"{type(exc).__name__}: {exc}",
            }
        raise


def _column_for(code):
    if str(code).startswith("6"):
        return "sse"
    if str(code).startswith(("0", "3")):
        return "szse"
    return "third"


def _announcement_page(code, org_id, start_date, end_date, page_num):
    form = {
        "stock": f"{code},{org_id}",
        "searchkey": "",
        "plate": "",
        "category": "",
        "trade": "",
        "column": _column_for(code),
        "pageNum": str(page_num),
        "pageSize": str(PAGE_SIZE),
        "tabName": "fulltext",
        "sortName": "",
        "sortType": "",
        "limit": "",
        "showTitle": "",
        "seDate": f"{start_date}~{end_date}",
        "isHLtitle": "true",
    }
    payload = _request_json(CNINFO_QUERY_URL, method="POST", form=form, timeout=10, attempts=2)
    rows = payload.get("announcements") or []
    if not isinstance(rows, list):
        rows = []
    total = payload.get("totalRecordNum")
    if total is None:
        total = payload.get("totalAnnouncement")
    try:
        total = int(total)
    except (TypeError, ValueError):
        total = len(rows)
    return rows, total


def _query_announcements(code, org_id, start_date, end_date):
    rows, total = _announcement_page(code, org_id, start_date, end_date, 1)
    pages_total = max(1, int(math.ceil(total / PAGE_SIZE))) if total else 1
    pages_requested = min(pages_total, MAX_PAGES)
    errors = []
    all_rows = list(rows)
    for page in range(2, pages_requested + 1):
        try:
            page_rows, _ = _announcement_page(code, org_id, start_date, end_date, page)
            all_rows.extend(page_rows)
        except Exception as exc:
            errors.append(f"page {page}: {type(exc).__name__}: {exc}")
            break

    complete = not errors and pages_total <= MAX_PAGES
    if pages_total > MAX_PAGES:
        errors.append(f"query capped at {MAX_PAGES}/{pages_total} pages")
    return all_rows, {
        "total_record_num": total,
        "pages_total": pages_total,
        "pages_requested": pages_requested,
        "rows_received": len(all_rows),
        "complete": complete,
        "errors": errors,
    }


def classify_event(title):
    title = _clean_title(title)
    for event_type, keywords in EVENT_RULES:
        if any(keyword in title for keyword in keywords):
            return event_type
    return "OTHER"


def _importance(event_type, title):
    title = str(title or "")
    if any(keyword in title for keyword in ("重大", "立案", "处罚", "终止", "中标", "风险提示")):
        return "HIGH"
    if event_type in HIGH_IMPORTANCE_TYPES:
        return "HIGH"
    if event_type in MEDIUM_IMPORTANCE_TYPES:
        return "MEDIUM"
    return "LOW"


def _extract_dates(text):
    values = []
    for match in _DATE_CN_RE.finditer(text or ""):
        try:
            value = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=CST).date().isoformat()
            if value not in values:
                values.append(value)
        except ValueError:
            continue
    for match in _DATE_ISO_RE.finditer(text or ""):
        try:
            value = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=CST).date().isoformat()
            if value not in values:
                values.append(value)
        except ValueError:
            continue
    return values


def _amount_value(number, unit):
    value = float(number)
    if unit == "亿元":
        return value * 100_000_000
    if unit == "万元":
        return value * 10_000
    return value


def _period_from_title(title):
    year_match = re.search(r"(20\d{2})年", title or "")
    if not year_match:
        return None
    year = year_match.group(1)
    if "半年度" in title or "半年报" in title:
        return f"{year}H1"
    if "一季度" in title:
        return f"{year}Q1"
    if "三季度" in title:
        return f"{year}Q3"
    if "年度" in title or "年报" in title:
        return f"{year}FY"
    return None


def extract_facts(event_type, title, api_snippet=None):
    source_text = " ".join(x for x in (_clean_title(title), _clean_title(api_snippet)) if x)
    amounts = []
    for match in _AMOUNT_RE.finditer(source_text):
        raw = match.group(0)
        entry = {
            "raw": raw,
            "value_yuan": round(_amount_value(match.group(1), match.group(2)), 2),
            "unit": match.group(2),
        }
        if entry not in amounts:
            amounts.append(entry)

    percentages = []
    for match in _PERCENT_RE.finditer(source_text):
        value = float(match.group(1))
        if value not in percentages:
            percentages.append(value)

    dates = _extract_dates(source_text)
    facts = {
        "extraction_scope": "TITLE_AND_API_SNIPPET" if api_snippet else "TITLE_ONLY",
        "amounts": amounts,
        "percentages": percentages,
        "dates": dates,
    }

    period = _period_from_title(title)
    if period:
        facts["period"] = period

    if event_type == "EARNINGS_FORECAST":
        yoy = _YOY_RANGE_RE.search(source_text)
        facts["profit_min_yuan"] = amounts[0]["value_yuan"] if len(amounts) >= 1 else None
        facts["profit_max_yuan"] = amounts[1]["value_yuan"] if len(amounts) >= 2 else None
        facts["yoy_min_percent"] = float(yoy.group(1)) if yoy else None
        facts["yoy_max_percent"] = float(yoy.group(2)) if yoy else None
    elif event_type == "BUYBACK":
        cap = _PRICE_CAP_RE.search(source_text)
        facts["amount_min_yuan"] = amounts[0]["value_yuan"] if len(amounts) >= 1 else None
        facts["amount_max_yuan"] = amounts[1]["value_yuan"] if len(amounts) >= 2 else None
        facts["price_cap_yuan_per_share"] = float(cap.group(1)) if cap else None
        if "完成" in source_text:
            facts["progress"] = "COMPLETED"
        elif "进展" in source_text or "实施" in source_text:
            facts["progress"] = "IN_PROGRESS"
        else:
            facts["progress"] = None
    elif event_type in {"HOLDER_INCREASE", "HOLDER_DECREASE"}:
        facts["share_percentages"] = percentages
    elif event_type == "UNLOCK":
        facts["unlock_date"] = dates[0] if dates else None
        facts["unlock_percentages"] = percentages
    elif event_type == "PLEDGE":
        facts["pledge_percentages"] = percentages
        facts["pledge_dates"] = dates
    elif event_type in {"CONVERTIBLE_BOND", "PREFERRED_SHARES", "REFINANCING"}:
        facts["potential_dilution_percentages"] = percentages
        facts["capital_amounts"] = amounts
        facts["effective_dates"] = dates
    elif event_type == "MAJOR_CONTRACT":
        facts["contract_amount_yuan"] = amounts[0]["value_yuan"] if amounts else None
    elif event_type == "DIVIDEND":
        facts["effective_dates"] = dates
    return facts


def _status_from_title(title):
    text = str(title or "")
    if any(word in text for word in ("终止", "取消")):
        return "CANCELLED"
    if any(word in text for word in ("回购完成", "实施完毕", "已完成", "完成公告")):
        return "COMPLETED"
    return "OPEN"


def _freshness(published_at, now):
    published = _parse_time(published_at)
    if not published:
        return "UNKNOWN"
    age_days = max(0, (now.date() - published.date()).days)
    if age_days <= 1:
        return "CURRENT"
    if age_days <= 7:
        return "RECENT_7D"
    if age_days <= 30:
        return "RECENT_30D"
    if age_days <= 90:
        return "RECENT_90D"
    return "HISTORICAL"


def _fallback_event_id(code, published_at, title):
    raw = f"{code}|{published_at}|{_clean_title(title)}".encode("utf-8")
    return "cninfo:sha256:" + hashlib.sha256(raw).hexdigest()[:24]


def _effective_date(facts, published_at):
    published = _parse_time(published_at)
    for value in facts.get("dates") or []:
        try:
            date = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            continue
        if published is None or date >= published.date():
            return date.isoformat()
    return None


def normalize_announcement(code, row, now):
    title = _clean_title(row.get("announcementTitle") or row.get("title"))
    published = _parse_time(row.get("announcementTime") or row.get("announcement_time"))
    published_at = _iso(published)
    event_type = classify_event(title)
    snippet = row.get("announcementContent") or row.get("announcementSummary")
    facts = extract_facts(event_type, title, snippet)
    announcement_id = row.get("announcementId") or row.get("announcement_id")
    event_id = f"cninfo:{announcement_id}" if announcement_id else _fallback_event_id(code, published_at, title)
    adjunct = row.get("adjunctUrl") or row.get("adjunct_url")
    source_url = CNINFO_STATIC_BASE + str(adjunct).lstrip("/") if adjunct else None
    return {
        "event_id": event_id,
        "code": str(row.get("secCode") or code).zfill(6),
        "event_type": event_type,
        "title": title,
        "published_at": published_at,
        "effective_date": _effective_date(facts, published_at),
        "source": "CNINFO",
        "source_tier": "OFFICIAL",
        "source_url": source_url,
        "source_document_id": str(announcement_id) if announcement_id else None,
        "importance": _importance(event_type, title),
        "facts": facts,
        "freshness": _freshness(published_at, now),
        "fetched_at": _iso(now),
        "status": _status_from_title(title),
        "related_event_id": None,
        "supersedes_event_id": None,
    }


def _series_key(title):
    text = _clean_title(title)
    text = _CORRECTION_RE.sub("", text)
    text = _PROGRESS_RE.sub("", text)
    text = re.sub(r"[（(].*?(更正|修订|补充|进展).*?[)）]", "", text)
    text = text.replace("关于", "").replace("公告", "")
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text)
    return text


def link_related_events(events):
    ordered = sorted(events, key=lambda x: x.get("published_at") or "")
    previous = []
    for event in ordered:
        title = event.get("title") or ""
        correction = bool(_CORRECTION_RE.search(title))
        progress = bool(_PROGRESS_RE.search(title))
        if correction or progress:
            key = _series_key(title)
            for candidate in reversed(previous):
                if candidate.get("event_type") != event.get("event_type"):
                    continue
                candidate_key = _series_key(candidate.get("title"))
                if not key or not candidate_key:
                    continue
                if key == candidate_key or key in candidate_key or candidate_key in key:
                    event["related_event_id"] = candidate.get("event_id")
                    if correction:
                        event["supersedes_event_id"] = candidate.get("event_id")
                    break
        previous.append(event)
    return events


def _event_sort_key(event):
    return event.get("published_at") or ""


def _merge_events(cached_events, fresh_events):
    merged = {}
    for event in cached_events or []:
        if isinstance(event, dict) and event.get("event_id"):
            merged[event["event_id"]] = dict(event)
    for event in fresh_events or []:
        if isinstance(event, dict) and event.get("event_id"):
            old = merged.get(event["event_id"]) or {}
            value = dict(old)
            value.update(event)
            merged[event["event_id"]] = value
    events = sorted(merged.values(), key=_event_sort_key, reverse=True)[:MAX_CACHED_EVENTS]
    return link_related_events(events)


def _filter_window(events, start_date):
    out = []
    for event in events or []:
        published = _parse_time(event.get("published_at"))
        if published and published.date() >= start_date:
            out.append(event)
    return sorted(out, key=_event_sort_key, reverse=True)


def _upcoming(events, now):
    out = []
    for event in events or []:
        value = event.get("effective_date")
        if not value:
            continue
        try:
            date = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            continue
        if date > now.date():
            out.append(event)
    return sorted(out, key=lambda x: (x.get("effective_date") or "", x.get("published_at") or ""))


def _cache_payload(code, org_id, events, covered_start_date, now, query_status):
    return {
        "schema_version": EVENT_CACHE_SCHEMA,
        "code": code,
        "org_id": org_id,
        "source": "CNINFO",
        "source_tier": "OFFICIAL",
        "covered_start_date": covered_start_date.isoformat(),
        "updated_at_cst": _iso(now),
        "query_status": query_status,
        "event_count": len(events),
        "events": events,
    }


def _resolve_query_start(cache, desired_start, now):
    covered_text = (cache or {}).get("covered_start_date")
    covered = None
    if covered_text:
        try:
            covered = datetime.strptime(str(covered_text), "%Y-%m-%d").date()
        except ValueError:
            pass
    if not covered or covered > desired_start:
        return desired_start, "FULL_WINDOW"

    cached_events = (cache or {}).get("events") or []
    latest = None
    for event in cached_events:
        published = _parse_time((event or {}).get("published_at"))
        if published and (latest is None or published > latest):
            latest = published
    if latest:
        overlap = latest.date() - timedelta(days=OVERLAP_REFRESH_DAYS)
        return max(desired_start, overlap), "INCREMENTAL_OVERLAP"
    return desired_start, "FULL_WINDOW"


def _event_context(events):
    by_type = {}
    high = []
    for event in events:
        event_type = event.get("event_type") or "OTHER"
        by_type[event_type] = by_type.get(event_type, 0) + 1
        if event.get("importance") == "HIGH":
            high.append(event.get("event_id"))
    return {
        "count": len(events),
        "by_type": dict(sorted(by_type.items())),
        "high_importance_event_ids": high,
        "latest_high_importance_event_id": high[0] if high else None,
    }


def fetch_events_for_code(code, lookback_days, now=None):
    now = now or datetime.now(CST)
    lookback_days = int(lookback_days or DEFAULT_LOOKBACK_DAYS)
    if lookback_days not in ALLOWED_LOOKBACK_DAYS:
        raise ValueError("lookback_days must be one of 7, 30, 90")

    desired_start = now.date() - timedelta(days=lookback_days)
    cache_path = _event_cache_path(code)
    cache = _read_json(cache_path) or {}
    cached_events = cache.get("events") if isinstance(cache.get("events"), list) else []
    org_id = cache.get("org_id")
    map_state = None

    try:
        if not org_id:
            stock_map, map_state = _load_stock_map(now)
            mapping = stock_map.get(code)
            if not mapping:
                raise RuntimeError(f"CNINFO orgId not found for {code}")
            org_id = mapping.get("org_id")

        query_start, refresh_mode = _resolve_query_start(cache, desired_start, now)
        rows, query_meta = _query_announcements(
            code,
            org_id,
            query_start.isoformat(),
            now.date().isoformat(),
        )
        fresh_events = [normalize_announcement(code, row, now) for row in rows if isinstance(row, dict)]
        merged = _merge_events(cached_events, fresh_events)
        covered_start = min(desired_start, query_start)
        query_status = "OK" if query_meta.get("complete") else "PARTIAL"
        _write_json(cache_path, _cache_payload(code, org_id, merged, covered_start, now, query_status))

        recent = _filter_window(merged, desired_start)
        upcoming = _upcoming(merged, now)
        status = "OK" if query_meta.get("complete") else "PARTIAL"
        return {
            "status": status,
            "source": "CNINFO",
            "source_tier": "OFFICIAL",
            "source_url": "https://www.cninfo.com.cn/",
            "lookback_days": lookback_days,
            "window_start_date": desired_start.isoformat(),
            "window_end_date": now.date().isoformat(),
            "fetched_at": _iso(now),
            "latest": recent[0] if recent else None,
            "recent": recent,
            "upcoming": upcoming,
            "event_context": _event_context(recent),
            "cache": {
                "state": "REFRESHED" if cached_events else "BOOTSTRAP",
                "path": str(cache_path),
                "refresh_mode": refresh_mode,
                "cached_event_count_before": len(cached_events),
                "merged_event_count": len(merged),
                "org_id_from_cache": bool(cache.get("org_id")),
                "stock_map": map_state,
            },
            "provider_health": {
                "status": status,
                "query": query_meta,
                "error": None if status == "OK" else "; ".join(query_meta.get("errors") or []),
            },
            "no_events_reason": "NO_ANNOUNCEMENTS_IN_WINDOW" if not recent else None,
            "error": None if status == "OK" else "; ".join(query_meta.get("errors") or []),
        }
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        recent = _filter_window(cached_events, desired_start)
        upcoming = _upcoming(cached_events, now)
        if cached_events:
            return {
                "status": "DEGRADED",
                "source": "CNINFO",
                "source_tier": "OFFICIAL",
                "source_url": "https://www.cninfo.com.cn/",
                "lookback_days": lookback_days,
                "window_start_date": desired_start.isoformat(),
                "window_end_date": now.date().isoformat(),
                "fetched_at": _iso(now),
                "latest": recent[0] if recent else None,
                "recent": recent,
                "upcoming": upcoming,
                "event_context": _event_context(recent),
                "cache": {
                    "state": "STALE_FALLBACK",
                    "path": str(cache_path),
                    "cached_event_count": len(cached_events),
                    "cache_updated_at_cst": cache.get("updated_at_cst"),
                    "stock_map": map_state,
                },
                "provider_health": {"status": "ERROR", "error": error},
                "no_events_reason": None,
                "error": error,
            }
        return {
            "status": "ERROR",
            "source": "CNINFO",
            "source_tier": "OFFICIAL",
            "source_url": "https://www.cninfo.com.cn/",
            "lookback_days": lookback_days,
            "window_start_date": desired_start.isoformat(),
            "window_end_date": now.date().isoformat(),
            "fetched_at": _iso(now),
            "latest": None,
            "recent": [],
            "upcoming": [],
            "event_context": _event_context([]),
            "cache": {"state": "MISS", "path": str(cache_path), "stock_map": map_state},
            "provider_health": {"status": "ERROR", "error": error},
            "no_events_reason": "PROVIDER_FAILED_NO_CACHE",
            "error": error,
        }


def finalize_snapshot(snapshot_path, config=None):
    path = Path(snapshot_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    config = config or {}
    lookback = int(config.get("event_lookback_days") or os.environ.get("EVENT_LOOKBACK_DAYS") or DEFAULT_LOOKBACK_DAYS)
    if lookback not in ALLOWED_LOOKBACK_DAYS:
        raise ValueError("event lookback must be one of 7, 30, 90")

    now = _parse_time(data.get("runner_time_utc")) or _parse_time(data.get("runner_time_cst")) or datetime.now(CST)
    ok = 0
    degraded = 0
    failed = 0
    total_events = 0
    for code, item in (data.get("detail_stocks") or {}).items():
        events = fetch_events_for_code(code, lookback, now=now)
        item["events"] = events
        item["event_context"] = events.get("event_context")
        status = events.get("status")
        if status == "OK":
            ok += 1
        elif status in {"PARTIAL", "DEGRADED"}:
            degraded += 1
        else:
            failed += 1
        total_events += len(events.get("recent") or [])

    data["schema_version"] = max(int(data.get("schema_version") or 0), 12)
    data.setdefault("features", {})["company_events"] = "v1"
    data["company_events"] = {
        "status": "ERROR" if failed and not (ok or degraded) else ("PARTIAL" if failed or degraded else "OK"),
        "source": "CNINFO",
        "source_tier": "OFFICIAL",
        "lookback_days": lookback,
        "detail_stock_count": len(data.get("detail_stocks") or {}),
        "ok_count": ok,
        "degraded_count": degraded,
        "error_count": failed,
        "recent_event_count": total_events,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "COMPANY_EVENTS "
        f"status={data['company_events']['status']} lookback={lookback} "
        f"ok={ok} degraded={degraded} error={failed} recent_events={total_events}",
        flush=True,
    )
    print("SNAPSHOT_SCHEMA_UPGRADED schema_version=12 feature=company_events:v1", flush=True)
