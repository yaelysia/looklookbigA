import json
import tempfile
from pathlib import Path

import config_security
import realtime_quotes_watchlist as base


def expect_reject(label, func):
    try:
        func()
    except ValueError:
        print(f"PASS reject {label}")
        return
    raise AssertionError(f"expected rejection: {label}")


def valid_config():
    return {
        "detail_codes": ["002558"],
        "light_codes": ["002555"],
        "groups": {
            "game": {
                "label": "game",
                "target_code": "002558",
                "member_codes": ["002555", "002517"],
            }
        },
        "max_total_codes": 10,
    }


def test_valid_config():
    normalized = config_security.validate_raw_config(base, valid_config())
    assert normalized["detail_codes"] == ["002558"]
    assert normalized["light_codes"] == ["002555", "002517"]
    assert normalized["truncated"] is False
    print("PASS valid_config")


def test_detail_cannot_bypass_total():
    raw = valid_config()
    raw["groups"] = {}
    raw["light_codes"] = []
    raw["detail_codes"] = [f"{i:06d}" for i in range(1, 12)]
    raw["max_total_codes"] = 10
    expect_reject(
        "detail exceeds max_total_codes",
        lambda: config_security.validate_raw_config(base, raw),
    )


def test_group_cannot_push_unique_total_over_limit():
    raw = {
        "detail_codes": ["002558"],
        "light_codes": [],
        "groups": {
            "g": {
                "target_code": "002558",
                "member_codes": ["002555", "002517", "002602"],
            }
        },
        "max_total_codes": 3,
    }
    expect_reject(
        "group pushes unique total over max_total_codes",
        lambda: config_security.validate_raw_config(base, raw),
    )


def test_hard_total_cap():
    raw = valid_config()
    raw["max_total_codes"] = config_security.HARD_MAX_TOTAL_CODES + 1
    expect_reject(
        "hard total cap",
        lambda: config_security.validate_raw_config(base, raw),
    )


def test_group_member_cap():
    raw = valid_config()
    raw["groups"]["game"]["member_codes"] = [
        f"{300000 + i:06d}" for i in range(config_security.MAX_MEMBERS_PER_GROUP + 1)
    ]
    expect_reject(
        "group member cap",
        lambda: config_security.validate_raw_config(base, raw),
    )


def test_duplicate_flood_rejected():
    raw = valid_config()
    raw["groups"] = {}
    raw["light_codes"] = []
    raw["detail_codes"] = ["002558"] * 20
    expect_reject(
        "duplicate stock entries",
        lambda: config_security.validate_raw_config(base, raw),
    )


def test_config_size_cap():
    oversized = "{" + (" " * config_security.MAX_CONFIG_BYTES) + "}"
    expect_reject(
        "config byte cap",
        lambda: config_security.parse_config_text(base, oversized, source="oversized"),
    )


def test_path_traversal_and_absolute_paths():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "caller"
        root.mkdir()
        (root / "config").mkdir()
        good = root / "config" / "quote_watchlist.json"
        good.write_text(json.dumps(valid_config()), encoding="utf-8")

        resolved = config_security.resolve_caller_config_path(
            root, "config/quote_watchlist.json"
        )
        assert resolved == good.resolve()
        print("PASS normal caller config path")

        expect_reject(
            "parent traversal",
            lambda: config_security.resolve_caller_config_path(root, "../secret.json"),
        )
        expect_reject(
            "absolute path",
            lambda: config_security.resolve_caller_config_path(root, "/etc/passwd"),
        )

        outside = Path(temp) / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        symlink = root / "config" / "escape.json"
        symlink.symlink_to(outside)
        expect_reject(
            "symlink escape",
            lambda: config_security.resolve_caller_config_path(
                root, "config/escape.json"
            ),
        )


def main():
    tests = [
        test_valid_config,
        test_detail_cannot_bypass_total,
        test_group_cannot_push_unique_total_over_limit,
        test_hard_total_cap,
        test_group_member_cap,
        test_duplicate_flood_rejected,
        test_config_size_cap,
        test_path_traversal_and_absolute_paths,
    ]
    for test in tests:
        test()
    print(f"CONFIG_SECURITY_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
