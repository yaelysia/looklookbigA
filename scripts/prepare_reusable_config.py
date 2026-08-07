import json
import os
import shutil
from pathlib import Path

import config_security
import realtime_quotes_watchlist as base


def prepare(caller_root, engine_config, config_path, config_json):
    caller_root = Path(caller_root)
    engine_config = Path(engine_config)
    inline = str(config_json or "").strip()

    if inline:
        raw, normalized = config_security.parse_config_text(
            base, inline, source="config_json"
        )
        engine_config.parent.mkdir(parents=True, exist_ok=True)
        engine_config.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        source = "inline-json"
    else:
        caller_path = config_security.resolve_caller_config_path(caller_root, config_path)
        if caller_path.is_file():
            text = caller_path.read_text(encoding="utf-8")
            raw, normalized = config_security.parse_config_text(
                base, text, source=f"caller config {caller_path}"
            )
            engine_config.parent.mkdir(parents=True, exist_ok=True)
            engine_config.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            source = f"caller:{caller_path.relative_to(caller_root.resolve())}"
        else:
            text = engine_config.read_text(encoding="utf-8")
            _, normalized = config_security.parse_config_text(
                base, text, source="engine default config"
            )
            source = "engine-default"

    print(f"REUSABLE_CONFIG source={source}")
    print(
        "REUSABLE_CONFIG_READY "
        f"detail={len(normalized['detail_codes'])} "
        f"light={len(normalized['light_codes'])} "
        f"groups={len(normalized['groups'])} "
        f"total={len(normalized['detail_codes']) + len(normalized['light_codes'])} "
        f"max_total={normalized['max_total_codes']}"
    )
    return normalized


def main():
    caller_root = os.environ.get("CALLER_ROOT", ".caller")
    engine_config = os.environ.get(
        "ENGINE_CONFIG", ".engine/config/quote_watchlist.json"
    )
    config_path = os.environ.get("CONFIG_PATH", "config/quote_watchlist.json")
    config_json = os.environ.get("CONFIG_JSON", "")
    prepare(caller_root, engine_config, config_path, config_json)


if __name__ == "__main__":
    main()
