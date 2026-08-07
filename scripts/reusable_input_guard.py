import argparse
import json
from pathlib import Path


MAX_CONFIG_BYTES = 64 * 1024
MAX_TOTAL_CODES = 100
MAX_GROUPS = 32
MAX_GROUP_MEMBERS = 100


def _normalize_code(code):
    value = str(code).strip()
    if not value.isdigit() or len(value) > 6:
        raise ValueError(f"invalid stock code: {code!r}")
    return value.zfill(6)


def _load_json_text(text, source):
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_CONFIG_BYTES:
        raise ValueError(
            f"{source} is too large: {len(encoded)} bytes > {MAX_CONFIG_BYTES} bytes"
        )
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"{source} must contain a JSON object")
    return parsed


def _read_json_file(path, source):
    raw = path.read_bytes()
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError(
            f"{source} is too large: {len(raw)} bytes > {MAX_CONFIG_BYTES} bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{source} must be UTF-8 JSON") from exc
    return _load_json_text(text, source)


def _validated_code_list(value, label, max_entries):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    if len(value) > max_entries:
        raise ValueError(f"{label} has too many entries: {len(value)} > {max_entries}")

    normalized = []
    seen = set()
    for raw_code in value:
        code = _normalize_code(raw_code)
        if code in seen:
            raise ValueError(f"{label} contains duplicate code: {code}")
        seen.add(code)
        normalized.append(code)
    return normalized


def validate_config(parsed):
    raw_max_total = parsed.get("max_total_codes", 50)
    if isinstance(raw_max_total, bool):
        raise ValueError("max_total_codes must be an integer")
    try:
        max_total = int(raw_max_total)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_total_codes must be an integer") from exc
    if max_total < 1 or max_total > MAX_TOTAL_CODES:
        raise ValueError(
            f"max_total_codes must be between 1 and {MAX_TOTAL_CODES}, got {max_total}"
        )

    detail = _validated_code_list(
        parsed.get("detail_codes", []), "detail_codes", MAX_TOTAL_CODES
    )
    light = _validated_code_list(
        parsed.get("light_codes", []), "light_codes", MAX_TOTAL_CODES
    )

    groups = parsed.get("groups", {}) or {}
    if not isinstance(groups, dict):
        raise ValueError("groups must be an object")
    if len(groups) > MAX_GROUPS:
        raise ValueError(f"too many groups: {len(groups)} > {MAX_GROUPS}")

    effective_codes = set(detail) | set(light)
    for group_id, group in groups.items():
        if not isinstance(group, dict):
            raise ValueError(f"group {group_id!r} must be an object")

        target = group.get("target_code")
        if target not in (None, ""):
            effective_codes.add(_normalize_code(target))

        members = _validated_code_list(
            group.get("member_codes", []),
            f"groups.{group_id}.member_codes",
            MAX_GROUP_MEMBERS,
        )
        effective_codes.update(members)

    if len(detail) > max_total:
        raise ValueError(
            f"detail_codes alone exceed max_total_codes: {len(detail)} > {max_total}"
        )
    if len(effective_codes) > max_total:
        raise ValueError(
            "effective unique stock count exceeds max_total_codes: "
            f"{len(effective_codes)} > {max_total}"
        )

    return {
        "detail_count": len(detail),
        "light_count": len(light),
        "group_count": len(groups),
        "effective_code_count": len(effective_codes),
        "max_total_codes": max_total,
    }


def resolve_and_write_config(
    caller_root,
    config_path,
    config_json,
    engine_default_path,
    output_path,
):
    caller_root = Path(caller_root).resolve()
    engine_default_path = Path(engine_default_path).resolve()
    output_path = Path(output_path).resolve()

    inline = (config_json or "").strip()
    requested_path = Path(config_path or "config/quote_watchlist.json")

    if requested_path.is_absolute():
        raise ValueError("config_path must be relative to the caller repository")

    caller_path = (caller_root / requested_path).resolve()
    try:
        caller_path.relative_to(caller_root)
    except ValueError as exc:
        raise ValueError("config_path escapes the caller repository") from exc

    if inline:
        parsed = _load_json_text(inline, "config_json")
        source = "inline-json"
    elif caller_path.is_file():
        parsed = _read_json_file(caller_path, f"config_path {requested_path}")
        source = f"caller:{requested_path.as_posix()}"
    else:
        parsed = _read_json_file(engine_default_path, "engine default config")
        source = "engine-default"

    stats = validate_config(parsed)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return source, stats


def main():
    parser = argparse.ArgumentParser(description="Validate reusable workflow watchlist input")
    parser.add_argument("--caller-root", required=True)
    parser.add_argument("--config-path", default="config/quote_watchlist.json")
    parser.add_argument("--config-json", default="")
    parser.add_argument("--engine-default", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source, stats = resolve_and_write_config(
        args.caller_root,
        args.config_path,
        args.config_json,
        args.engine_default,
        args.output,
    )
    print(f"REUSABLE_CONFIG source={source}")
    print(
        "REUSABLE_CONFIG_READY "
        f"detail={stats['detail_count']} "
        f"light={stats['light_count']} "
        f"groups={stats['group_count']} "
        f"effective={stats['effective_code_count']} "
        f"max_total={stats['max_total_codes']}"
    )


if __name__ == "__main__":
    main()
