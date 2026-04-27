from __future__ import annotations

import os
import re
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
LOCAL_SETTINGS_FILES = (".env", "local_settings.env")


def _clean_env_value(value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        return cleaned[1:-1]
    return cleaned


def load_local_settings(root_dir: Path = ROOT_DIR) -> None:
    for filename in LOCAL_SETTINGS_FILES:
        path = root_dir / filename
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            os.environ.setdefault(key, _clean_env_value(value))
