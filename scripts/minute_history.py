import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import market_calendar


CAPTURES = {}
CAPTURE_LOCK = threading.Lock()
BASE = None


def _history_root():
    return Path(os.environ.get("MARKET_HISTORY_DIR", ".market-data/history"))


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        raise ValueError(f"invalid minute history JSON: {path}") from exc


def _snapshot_time(data):
    value = data.get("runner_time_cst")
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("snapshot runner_time_cst is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=market_calendar.CST)
    return parsed.astimezone(market_calendar.CST)


def _session_date(value):
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid minute session date: {value!r}") from exc


def _code(value):
    text = str(value or "").strip()
    if len(text) != 6 or not text.isdigit():
        raise ValueError(f"invalid minute history stock code: {value!r}")
    return text


def _time_label(value):
    text = str(value or "").replace(":", "").strip()
    try:
        datetime.strptime(text, "%H%M")
    except ValueError:
        return None
    return text


def _identity_key(observation):
    return (
        str(observation.get("runner_time_cst") or ""),
        str(observation.get("run_id") or ""),
        int(observation.get("run_attempt") or 0),
        str(observation.get("head_sha") or "").lower(),
    )


def _verified_identity(observation):
    run_id = observation.get("run_id")
    attempt = observation.get("run_attempt")
    head_sha = str(observation.get("head_sha") or "").lower()
    return (
        isinstance(run_id, int)
        and run_id > 0
        and isinstance(attempt, int)
        and attempt > 0
        and len(head_sha) == 40
        and all(ch in "0123456789abcdef" for ch in head_sha)
    )


def _point_digest(point):
    payload = json.dumps(point, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_points(points, expected_times):
    expected = set(expected_times)
    by_time = {}
    duplicates = set()
    unexpected = set()
    invalid = 0
    for raw in points or []:
        if not isinstance(raw, dict):
            invalid += 1
            continue
        label = _time_label(raw.get("time"))
        if label is None:
            invalid += 1
            continue
        if label not in expected:
            unexpected.add(label)
            continue
        point = dict(raw)
        point["time"] = label
        if label in by_time:
            duplicates.add(label)
        by_time[label] = point
    return by_time, {
        "duplicate_times": sorted(duplicates),
        "unexpected_times": sorted(unexpected),
        "invalid_row_count": invalid,
    }


def _latest_expectation(left, right, prefer):
    left_at = str((left or {}).get("evaluated_at_cst") or "")
    right_at = str((right or {}).get("evaluated_at_cst") or "")
    if right_at > left_at:
        return dict(right or {})
    if left_at > right_at:
        return dict(left or {})
    return dict(right if prefer == "incoming" else left or {})


def _merge_anomalies(left, right):
    left = left or {}
    right = right or {}
    return {
        "duplicate_times": sorted(set(left.get("duplicate_times") or []) | set(right.get("duplicate_times") or [])),
        "unexpected_times": sorted(set(left.get("unexpected_times") or []) | set(right.get("unexpected_times") or [])),
        "invalid_row_count": int(left.get("invalid_row_count") or 0) + int(right.get("invalid_row_count") or 0),
    }


def _recompute(document):
    expectation = document.get("calendar_expectation") or {}
    expected = list(expectation.get("expected_times") or [])
    points_by_time = {
        point.get("time"): point
        for point in document.get("points") or []
        if isinstance(point, dict) and point.get("time")
    }
    document["points"] = [points_by_time[label] for label in expected if label in points_by_time]
    missing = [label for label in expected if label not in points_by_time]

    cumulative_errors = []
    previous_volume = None
    previous_amount = None
    for point in document["points"]:
        try:
            volume = float(point.get("cum_volume"))
            amount = float(point.get("cum_amount"))
        except (TypeError, ValueError):
            cumulative_errors.append(point.get("time"))
            continue
        if previous_volume is not None and (volume < previous_volume or amount < previous_amount):
            cumulative_errors.append(point.get("time"))
        previous_volume = volume
        previous_amount = amount

    anomalies = document.get("source_anomalies") or {}
    conflicts = document.get("conflicts") or []
    coverage_status = "COMPLETE" if not missing else "PARTIAL"
    document["coverage"] = {
        "status": coverage_status,
        "observed_count": len(document["points"]),
        "expected_count": len(expected),
        "missing_count": len(missing),
        "missing_times": missing,
        "first_time": document["points"][0]["time"] if document["points"] else None,
        "last_time": document["points"][-1]["time"] if document["points"] else None,
    }
    continuity_reasons = []
    if missing:
        continuity_reasons.append("MISSING_EXPECTED_MINUTES")
    if cumulative_errors:
        continuity_reasons.append("NON_MONOTONIC_CUMULATIVE_FIELDS")
    if anomalies.get("duplicate_times"):
        continuity_reasons.append("DUPLICATE_SOURCE_MINUTE_TIMES")
    if int(anomalies.get("invalid_row_count") or 0) > 0:
        continuity_reasons.append("INVALID_SOURCE_MINUTE_ROWS")
    document["continuity"] = {
        "status": "PASS" if not continuity_reasons else "FAIL",
        "reason_codes": continuity_reasons,
        "cumulative_error_times": sorted(set(cumulative_errors)),
    }

    final_reasons = []
    if expectation.get("verification_status") != "VERIFIED":
        final_reasons.append("CALENDAR_UNVERIFIED")
    if expectation.get("status") != "COMPLETE_SESSION":
        final_reasons.append("SESSION_NOT_COMPLETE")
    if missing:
        final_reasons.append("MISSING_EXPECTED_MINUTES")
    if conflicts:
        final_reasons.append("CONFLICTING_MINUTE_REVISIONS")
    document["finality"] = {
        "status": "FINAL" if not final_reasons else "PROVISIONAL",
        "reason_codes": final_reasons,
    }

    replay_reasons = list(final_reasons)
    replay_reasons.extend(continuity_reasons)
    if not any(_verified_identity(item) for item in document.get("observations") or []):
        replay_reasons.append("OBSERVATION_IDENTITY_UNVERIFIED")
    replay_reasons = list(dict.fromkeys(replay_reasons))
    document["replay"] = {
        "contract_version": "v1",
        "eligible": not replay_reasons,
        "reason_codes": replay_reasons,
    }
    document["status"] = (
        "REPLAY_READY"
        if document["replay"]["eligible"]
        else ("IN_PROGRESS" if expectation.get("status") == "IN_PROGRESS" else "PARTIAL")
    )
    return document


def merge_documents(current, incoming, prefer="incoming"):
    if prefer not in {"current", "incoming"}:
        raise ValueError("minute history merge preference must be current or incoming")
    if not isinstance(current, dict) or not isinstance(incoming, dict):
        raise ValueError("minute history merge requires two documents")
    if current.get("code") != incoming.get("code") or current.get("session_date") != incoming.get("session_date"):
        raise ValueError("minute history documents identify different stock sessions")

    current_points = {point["time"]: point for point in current.get("points") or []}
    incoming_points = {point["time"]: point for point in incoming.get("points") or []}
    merged_points = dict(current_points)
    conflicts = list(current.get("conflicts") or []) + list(incoming.get("conflicts") or [])
    conflict_keys = {
        (item.get("time"), item.get("current_digest"), item.get("incoming_digest"))
        for item in conflicts
    }
    for label, point in incoming_points.items():
        previous = current_points.get(label)
        if previous is None:
            merged_points[label] = point
            continue
        if previous == point:
            continue
        conflict = {
            "time": label,
            "current_digest": _point_digest(previous),
            "incoming_digest": _point_digest(point),
        }
        key = (conflict["time"], conflict["current_digest"], conflict["incoming_digest"])
        if key not in conflict_keys:
            conflicts.append(conflict)
            conflict_keys.add(key)
        if prefer == "incoming":
            merged_points[label] = point

    observations = {}
    for item in list(current.get("observations") or []) + list(incoming.get("observations") or []):
        observations[_identity_key(item)] = item
    expectation = _latest_expectation(
        current.get("calendar_expectation"),
        incoming.get("calendar_expectation"),
        prefer,
    )
    winner = incoming if prefer == "incoming" else current
    merged = {
        "schema_version": 1,
        "record_id": current.get("record_id"),
        "market": "CN_A",
        "code": current.get("code"),
        "session_date": current.get("session_date"),
        "source": winner.get("source") or current.get("source") or incoming.get("source"),
        "calendar_expectation": expectation,
        "points": list(merged_points.values()),
        "observations": sorted(observations.values(), key=_identity_key),
        "source_anomalies": _merge_anomalies(
            current.get("source_anomalies"), incoming.get("source_anomalies")
        ),
        "conflicts": conflicts,
        "updated_at_cst": max(
            str(current.get("updated_at_cst") or ""),
            str(incoming.get("updated_at_cst") or ""),
        ),
    }
    return _recompute(merged)


def build_document(code, session_date, parsed_points, snapshot):
    code = _code(code)
    session_date = _session_date(session_date)
    now = _snapshot_time(snapshot)
    expected = market_calendar.expected_minute_times(now, session_date=session_date)
    points, anomalies = _normalize_points(parsed_points, expected.get("expected_times") or [])
    observation = dict(snapshot.get("observation") or {})
    observation["runner_time_cst"] = snapshot.get("runner_time_cst")
    observation["runner_time_utc"] = snapshot.get("runner_time_utc")
    observation["captured_point_count"] = len(points)
    document = {
        "schema_version": 1,
        "record_id": f"CN_A:{session_date}:{code}",
        "market": "CN_A",
        "code": code,
        "session_date": session_date,
        "source": "Tencent 1-minute cumulative series",
        "calendar_expectation": {
            "calendar_version": market_calendar.load_calendar()["calendar_version"],
            "evaluated_at_cst": now.isoformat(timespec="seconds"),
            "status": expected.get("status"),
            "verification_status": expected.get("verification_status"),
            "forming_minute": expected.get("forming_minute"),
            "expected_times": expected.get("expected_times") or [],
        },
        "points": list(points.values()),
        "observations": [observation],
        "source_anomalies": anomalies,
        "conflicts": [],
        "updated_at_cst": snapshot.get("runner_time_cst"),
    }
    return _recompute(document)


def _relative_path(code, session_date):
    return Path("minutes") / _session_date(session_date) / f"{_code(code)}.json"


def _locator(document, rel, observation):
    return {
        "status": document.get("status"),
        "record_id": document.get("record_id"),
        "path": rel.as_posix(),
        "session_date": document.get("session_date"),
        "observed_point_count": (document.get("coverage") or {}).get("observed_count"),
        "expected_point_count": (document.get("coverage") or {}).get("expected_count"),
        "finality": document.get("finality"),
        "replay": document.get("replay"),
        "observation": {
            "runner_time_cst": observation.get("runner_time_cst"),
            "run_id": observation.get("run_id"),
            "run_attempt": observation.get("run_attempt"),
            "head_sha": observation.get("head_sha"),
        },
    }


def install(base):
    global BASE
    if getattr(base, "_minute_history_installed", False):
        return
    original = base.tencent_minutes

    def captured_tencent_minutes(tcode):
        session_date, rows = original(tcode)
        parsed = base.parse_minutes(rows)
        with CAPTURE_LOCK:
            CAPTURES[tcode] = {
                "session_date": session_date,
                "points": parsed,
            }
        return session_date, rows

    base.tencent_minutes = captured_tencent_minutes
    base._minute_history_installed = True
    BASE = base


def finalize_snapshot(snapshot_path):
    if BASE is None:
        raise RuntimeError("minute history capture is not installed")
    path = Path(snapshot_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    records = {}
    observation = dict(snapshot.get("observation") or {})
    observation.setdefault("runner_time_cst", snapshot.get("runner_time_cst"))
    observation.setdefault("runner_time_utc", snapshot.get("runner_time_utc"))
    for code, item in (snapshot.get("detail_stocks") or {}).items():
        _, _, tcode = BASE.infer_identifiers(code)
        with CAPTURE_LOCK:
            capture = CAPTURES.get(tcode)
        if not capture:
            locator = {
                "status": "NO_CAPTURE",
                "record_id": None,
                "path": None,
                "session_date": None,
                "observed_point_count": 0,
                "expected_point_count": None,
                "finality": {"status": "PROVISIONAL", "reason_codes": ["MINUTE_CAPTURE_MISSING"]},
                "replay": {"contract_version": "v1", "eligible": False, "reason_codes": ["MINUTE_CAPTURE_MISSING"]},
                "observation": observation,
            }
        else:
            session_date = _session_date(capture.get("session_date"))
            rel = _relative_path(code, session_date)
            canonical_path = _history_root() / rel
            incoming = build_document(code, session_date, capture.get("points"), snapshot)
            existing = _load_json(canonical_path)
            document = merge_documents(existing, incoming, prefer="incoming") if existing else incoming
            _write_json(canonical_path, document)
            locator = _locator(document, rel, observation)
        item["minute_history"] = locator
        records[code] = locator

    snapshot["minute_history"] = {
        "schema_version": 1,
        "storage": "market-data history/minutes/<session-date>/<code>.json",
        "records": records,
        "replay_eligible_codes": sorted(
            code for code, locator in records.items() if (locator.get("replay") or {}).get("eligible")
        ),
    }
    snapshot["schema_version"] = max(int(snapshot.get("schema_version") or 0), 20)
    snapshot.setdefault("features", {})["complete_minute_history"] = "v1"
    _write_json(path, snapshot)
    summary = ",".join(
        f"{code}:{locator.get('status')}:{locator.get('observed_point_count')}/"
        f"{locator.get('expected_point_count')}"
        for code, locator in sorted(records.items())
    )
    print(f"MINUTE_HISTORY records=[{summary}]", flush=True)


def _safe_path(root, rel):
    root = Path(root).resolve()
    candidate = (root / str(rel)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("minute history locator escapes history root") from exc
    return candidate


def load_day(code, session_date, history_root=None, require_replay_eligible=False):
    root = Path(history_root) if history_root is not None else _history_root()
    path = _safe_path(root, _relative_path(code, session_date))
    document = _load_json(path)
    if not isinstance(document, dict):
        raise FileNotFoundError(path)
    if document.get("code") != _code(code) or document.get("session_date") != _session_date(session_date):
        raise ValueError("minute history document identity mismatch")
    if require_replay_eligible and not (document.get("replay") or {}).get("eligible"):
        raise RuntimeError(
            "minute history is not replay eligible: "
            + ",".join((document.get("replay") or {}).get("reason_codes") or [])
        )
    return document


def load_from_snapshot(snapshot, code, history_root=None, require_replay_eligible=False):
    if isinstance(snapshot, (str, Path)):
        snapshot = json.loads(Path(snapshot).read_text(encoding="utf-8"))
    locator = (((snapshot.get("detail_stocks") or {}).get(_code(code)) or {}).get("minute_history") or {})
    if not locator.get("path"):
        raise FileNotFoundError(f"snapshot has no minute history locator for {code}")
    locator_path = Path(str(locator["path"]))
    if not locator_path.parts or locator_path.parts[0] != "minutes":
        raise ValueError("snapshot minute history locator is outside the minutes namespace")
    root = Path(history_root) if history_root is not None else _history_root()
    path = _safe_path(root, locator_path)
    document = _load_json(path)
    if not isinstance(document, dict):
        raise FileNotFoundError(path)
    if document.get("record_id") != locator.get("record_id"):
        raise ValueError("snapshot minute history locator identity mismatch")
    if (document.get("coverage") or {}).get("observed_count", 0) < int(locator.get("observed_point_count") or 0):
        raise ValueError("minute history document regressed behind snapshot locator")
    expected_observation = locator.get("observation") or {}
    expected_key = _identity_key(expected_observation)
    if expected_key not in {_identity_key(item) for item in document.get("observations") or []}:
        raise ValueError("minute history document does not contain snapshot observation")
    if require_replay_eligible and not (document.get("replay") or {}).get("eligible"):
        raise RuntimeError("snapshot-located minute history is not replay eligible")
    return document
