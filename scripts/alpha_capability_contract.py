import json
from pathlib import Path


DEFAULT_MANIFEST = Path("contracts/looklookalpha-provider-v1.json")


class CapabilityContractError(RuntimeError):
    pass


def load_manifest(path=DEFAULT_MANIFEST):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _expect_mapping(value, path, errors):
    if not isinstance(value, dict):
        errors.append(f"{path}: expected object")
        return {}
    return value


def validate_snapshot(snapshot, manifest=None):
    manifest = manifest or load_manifest()
    errors = []
    snapshot_contract = manifest.get("snapshot") or {}

    minimum_schema = int(snapshot_contract.get("minimum_schema_version") or 0)
    actual_schema = int(snapshot.get("schema_version") or 0)
    if actual_schema < minimum_schema:
        errors.append(
            f"schema_version: expected >= {minimum_schema}, got {actual_schema}"
        )

    features = _expect_mapping(snapshot.get("features"), "features", errors)
    for name, expected_version in (snapshot_contract.get("required_features") or {}).items():
        actual_version = features.get(name)
        if actual_version != expected_version:
            errors.append(
                f"features.{name}: expected {expected_version!r}, got {actual_version!r}"
            )

    for name in snapshot_contract.get("required_top_level_objects") or []:
        value = snapshot.get(name)
        if value is None:
            errors.append(f"{name}: missing")
        elif not isinstance(value, dict):
            errors.append(f"{name}: expected object")

    detail = _expect_mapping(snapshot.get("detail_stocks"), "detail_stocks", errors)
    if not detail:
        errors.append("detail_stocks: at least one detail stock is required")
    required_detail = snapshot_contract.get("required_detail_objects") or []
    for code, item in sorted(detail.items()):
        item = _expect_mapping(item, f"detail_stocks.{code}", errors)
        for name in required_detail:
            value = item.get(name)
            if value is None:
                errors.append(f"detail_stocks.{code}.{name}: missing")
            elif not isinstance(value, dict):
                errors.append(f"detail_stocks.{code}.{name}: expected object")

        intraday = item.get("intraday") or {}
        guard = intraday.get("current_price_guard") or {}
        if not guard:
            errors.append(
                f"detail_stocks.{code}.intraday.current_price_guard: missing"
            )

    live_guard = snapshot.get("live_price_guard") or {}
    policy = live_guard.get("policy") or {}
    if policy.get("historical_sources_allowed_for_current_price") is not False:
        errors.append(
            "live_price_guard.policy.historical_sources_allowed_for_current_price must be false"
        )

    return errors


def assert_snapshot_compatible(snapshot, manifest=None):
    errors = validate_snapshot(snapshot, manifest)
    if errors:
        raise CapabilityContractError("; ".join(errors))
    return True


def validate_snapshot_file(snapshot_path, manifest_path=DEFAULT_MANIFEST):
    snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    manifest = load_manifest(manifest_path)
    assert_snapshot_compatible(snapshot, manifest)
    return snapshot


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Validate snapshot.json against the stable looklookAlpha provider capability contract."
    )
    parser.add_argument("snapshot", nargs="?", default="snapshot.json")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    args = parser.parse_args(argv)

    snapshot = validate_snapshot_file(args.snapshot, args.manifest)
    print(
        "ALPHA_CAPABILITY_CONTRACT "
        f"status=PASS schema_version={snapshot.get('schema_version')} "
        f"detail_stocks={len(snapshot.get('detail_stocks') or {})}"
    )


if __name__ == "__main__":
    main()
