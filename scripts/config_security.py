import json
import os
import re
from pathlib import Path

MAX_CONFIG_BYTES = 32 * 1024
HARD_MAX_TOTAL_CODES = 50
MAX_GROUPS = 20
MAX_MEMBERS_PER_GROUP = 50
MAX_RAW_CODE_ENTRIES = 200
MAX_GROUP_ID_CHARS = 64
MAX_GROUP_LABEL_CHARS = 120
MAX_CONFIG_PATH_CHARS = 256

_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _require_list(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _normalize_unique_codes(base, values, field):
    out = []
    seen = set()
    for raw_code in values:
        code = base.normalize_code(raw_code)
        if code in seen:
            raise ValueError(f"{field} contains duplicate stock code: {code}")
        seen.add(code)
        out.append(code)
    return out


def _parse_max_total(raw):
    value = raw.get("max_total_codes", HARD_MAX_TOTAL_CODES)
    if isinstance(value, bool):
        raise ValueError("max_total_codes must be an integer")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError("max_total_codes must be an integer") from None
    if value < 1 or value > HARD_MAX_TOTAL_CODES:
        raise ValueError(
            f"max_total_codes must be between 1 and {HARD_MAX_TOTAL_CODES}"
        )
    return value


def validate_raw_config(base, raw):
    """Validate an untrusted watchlist object and return the normalized runtime config."""
    if not isinstance(raw, dict):
        raise ValueError("watchlist config must be a JSON object")

    max_total = _parse_max_total(raw)
    raw_detail = _require_list(raw.get("detail_codes", []), "detail_codes")
    raw_light = _require_list(raw.get("light_codes", []), "light_codes")
    raw_groups = raw.get("groups", {}) or {}
    if not isinstance(raw_groups, dict):
        raise ValueError("groups must be an object")
    if len(raw_groups) > MAX_GROUPS:
        raise ValueError(f"groups exceeds hard limit of {MAX_GROUPS}")

    raw_entry_count = len(raw_detail) + len(raw_light)
    for group_id, group in raw_groups.items():
        if not isinstance(group, dict):
            raise ValueError(f"group {group_id!r} must be an object")
        members = _require_list(group.get("member_codes", []), f"groups.{group_id}.member_codes")
        if len(members) > MAX_MEMBERS_PER_GROUP:
            raise ValueError(
                f"groups.{group_id}.member_codes exceeds hard limit of {MAX_MEMBERS_PER_GROUP}"
            )
        raw_entry_count += len(members) + (1 if group.get("target_code") else 0)

    if raw_entry_count > MAX_RAW_CODE_ENTRIES:
        raise ValueError(
            f"raw stock-code entries exceed hard limit of {MAX_RAW_CODE_ENTRIES}"
        )

    detail = _normalize_unique_codes(base, raw_detail, "detail_codes")
    standalone_light = _normalize_unique_codes(base, raw_light, "light_codes")

    if len(detail) > max_total:
        raise ValueError(
            f"detail_codes count {len(detail)} exceeds max_total_codes={max_total}"
        )

    groups = {}
    group_light_candidates = []
    for raw_group_id, group in raw_groups.items():
        group_id = str(raw_group_id)
        if (
            not group_id
            or len(group_id) > MAX_GROUP_ID_CHARS
            or not _GROUP_ID_RE.fullmatch(group_id)
        ):
            raise ValueError(
                f"invalid group id {group_id!r}; use 1-{MAX_GROUP_ID_CHARS} chars from A-Z, a-z, 0-9, _, -, ."
            )

        label = str(group.get("label") or group_id)
        if len(label) > MAX_GROUP_LABEL_CHARS:
            raise ValueError(
                f"groups.{group_id}.label exceeds {MAX_GROUP_LABEL_CHARS} characters"
            )

        target = base.normalize_code(group["target_code"]) if group.get("target_code") else None
        members = _normalize_unique_codes(
            base,
            _require_list(group.get("member_codes", []), f"groups.{group_id}.member_codes"),
            f"groups.{group_id}.member_codes",
        )
        if target and target in members:
            raise ValueError(f"groups.{group_id}.member_codes must not repeat target_code {target}")

        for code in members:
            if code not in detail and code not in group_light_candidates:
                group_light_candidates.append(code)
        if target and target not in detail and target not in group_light_candidates:
            group_light_candidates.append(target)

        groups[group_id] = {
            "label": label,
            "target_code": target,
            "member_codes": members,
        }

    light = []
    for code in group_light_candidates + standalone_light:
        if code not in detail and code not in light:
            light.append(code)

    total_unique = len(detail) + len(light)
    if total_unique > max_total:
        raise ValueError(
            f"unique stock count {total_unique} exceeds max_total_codes={max_total}; "
            "detail_codes are included in the total limit"
        )

    active_codes = set(detail) | set(light)
    for group in groups.values():
        group["active_member_codes"] = [
            code for code in group["member_codes"] if code in active_codes
        ]
        group["members_truncated"] = False

    return {
        "detail_codes": detail,
        "light_codes": light,
        "groups": groups,
        "max_total_codes": max_total,
        "truncated": False,
        "security_limits": {
            "hard_max_total_codes": HARD_MAX_TOTAL_CODES,
            "max_groups": MAX_GROUPS,
            "max_members_per_group": MAX_MEMBERS_PER_GROUP,
            "max_raw_code_entries": MAX_RAW_CODE_ENTRIES,
            "max_config_bytes": MAX_CONFIG_BYTES,
        },
    }


def parse_config_text(base, text, source="watchlist config"):
    if not isinstance(text, str):
        raise ValueError(f"{source} must be text")
    size = len(text.encode("utf-8"))
    if size > MAX_CONFIG_BYTES:
        raise ValueError(
            f"{source} is {size} bytes; maximum allowed is {MAX_CONFIG_BYTES} bytes"
        )
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} is not valid JSON: {exc}") from None
    normalized = validate_raw_config(base, raw)
    return raw, normalized


def read_config_file(base, path):
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot stat watchlist config {path}: {exc}") from None
    if size > MAX_CONFIG_BYTES:
        raise ValueError(
            f"watchlist config is {size} bytes; maximum allowed is {MAX_CONFIG_BYTES} bytes"
        )
    text = path.read_text(encoding="utf-8")
    _, normalized = parse_config_text(base, text, source=str(path))
    return normalized


def resolve_caller_config_path(caller_root, config_path):
    """Resolve a caller-owned relative config path without allowing path/symlink escape."""
    raw = str(config_path or "config/quote_watchlist.json")
    if not raw or len(raw) > MAX_CONFIG_PATH_CHARS or "\x00" in raw:
        raise ValueError("config_path is empty, contains NUL, or is too long")

    candidate_input = Path(raw)
    if candidate_input.is_absolute():
        raise ValueError("config_path must be relative to the caller repository")

    root = Path(caller_root).resolve(strict=True)
    candidate = (root / candidate_input).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError("config_path escapes the caller repository") from None

    # If it exists, resolve(strict=True) again so a symlink cannot escape the root.
    if candidate.exists():
        resolved_existing = candidate.resolve(strict=True)
        try:
            resolved_existing.relative_to(root)
        except ValueError:
            raise ValueError("config_path symlink escapes the caller repository") from None
        candidate = resolved_existing
    return candidate


def install(base):
    """Replace the base loader so every runner invocation enforces the same hard limits."""
    original_path = base.CONFIG_PATH

    def secure_load_config():
        return read_config_file(base, original_path)

    base.load_config = secure_load_config
    base.CONFIG_SECURITY_LIMITS = {
        "max_config_bytes": MAX_CONFIG_BYTES,
        "hard_max_total_codes": HARD_MAX_TOTAL_CODES,
        "max_groups": MAX_GROUPS,
        "max_members_per_group": MAX_MEMBERS_PER_GROUP,
        "max_raw_code_entries": MAX_RAW_CODE_ENTRIES,
    }
