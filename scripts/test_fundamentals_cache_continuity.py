import os
import tempfile

import fundamentals_cache_continuity
import fundamentals_context as fundamentals


fundamentals_cache_continuity.install(fundamentals)


def test_partial_full_refresh_keeps_cached_missing_report_class():
    old_root = os.environ.get("MARKET_HISTORY_DIR")
    original = fundamentals_cache_continuity.__dict__.get("_unused")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["MARKET_HISTORY_DIR"] = tmp
        fundamentals._write_json(fundamentals._cache_path("002558"), {
            "raw": {
                "main": [{"REPORTDATE": "2026-06-30", "TOTAL_OPERATE_INCOME": 100}],
                "income": [{"REPORT_DATE": "2026-06-30", "PARENT_NETPROFIT": 10}],
                "balance": [{"REPORT_DATE": "2026-06-30", "TOTAL_ASSETS": 1000}],
                "cashflow": [{"REPORT_DATE": "2026-06-30", "NETCASH_OPERATE": 8}],
            },
            "source_urls": {"balance": "https://old.example/balance"},
        })

        # Replace the already-wrapped function's captured upstream behavior by
        # installing against a lightweight module clone is overkill; instead
        # validate the same merge contract with the public cache/fetch wrapper
        # through a temporary second module-like object.
        class Module:
            REPORTS = fundamentals.REPORTS
            _load_json = staticmethod(fundamentals._load_json)
            _cache_path = staticmethod(fundamentals._cache_path)
            _report_cache_continuity_installed = False

            @staticmethod
            def fetch_all_reports(base, code):
                return (
                    {
                        "main": [{"REPORTDATE": "2026-06-30", "TOTAL_OPERATE_INCOME": 110}],
                        "income": [{"REPORT_DATE": "2026-06-30", "PARENT_NETPROFIT": 11}],
                        "balance": [],
                        "cashflow": [{"REPORT_DATE": "2026-06-30", "NETCASH_OPERATE": 9}],
                    },
                    {"main": "https://new.example/main"},
                    ["balance: synthetic provider failure"],
                )

        fundamentals_cache_continuity.install(Module)
        raw, urls, errors = Module.fetch_all_reports(None, "002558")
        assert raw["main"][0]["TOTAL_OPERATE_INCOME"] == 110
        assert raw["balance"][0]["TOTAL_ASSETS"] == 1000
        assert urls["balance"] == "https://old.example/balance"
        assert any("CACHE_FALLBACK_REPORT_CLASSES=balance" in x for x in errors)

    if old_root is None:
        os.environ.pop("MARKET_HISTORY_DIR", None)
    else:
        os.environ["MARKET_HISTORY_DIR"] = old_root


def main():
    test_partial_full_refresh_keeps_cached_missing_report_class()
    print("PASS test_partial_full_refresh_keeps_cached_missing_report_class")
    print("FUNDAMENTALS_CACHE_CONTINUITY_TESTS passed=1")


if __name__ == "__main__":
    main()
