"""PhonyScreen settings, stored in %APPDATA%\\PhonyScreen\\config.json."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("APPDATA", Path.home())) / "PhonyScreen" / "config.json"
OLD_CONFIG_PATH = CONFIG_PATH.parent.parent / "PhonePane" / "config.json"  # the app's former name


def load_config() -> dict:
    if not CONFIG_PATH.exists() and OLD_CONFIG_PATH.exists():  # keep the paired phone and settings
        with contextlib.suppress(Exception):
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(OLD_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    with contextlib.suppress(Exception):
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def update_config(**values: object) -> None:
    """Merge values into the saved settings (re-reads first, so concurrent writers don't clobber)."""
    cfg = load_config()
    cfg.update(values)
    save_config(cfg)
