"""Persistent GUI settings stored as JSON in the user config dir."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

APP_NAME = "parse-pdf-gui"


def _user_config_dir() -> Path:
    """Cross-platform user config directory.

    Uses platformdirs if available, otherwise falls back to a sensible
    per-platform default.
    """
    try:
        from platformdirs import user_config_dir  # type: ignore

        return Path(user_config_dir(APP_NAME))
    except Exception:
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / APP_NAME
        if os.name == "nt":
            base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
            return Path(base) / APP_NAME
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(base) / APP_NAME


CONFIG_DIR = _user_config_dir()
SETTINGS_PATH = CONFIG_DIR / "settings.json"
SCRIPT_COPY_PATH = CONFIG_DIR / "parse_pdf.py"

DEFAULTS: dict[str, Any] = {
    "last_pdf_dir": str(Path.home()),
    "last_pdf_path": "",
    "out_dir": "out",
    "page_mode": "all",            # "all" | "pages"
    "pages": "",
    "search": "",
    # Vision features default to ON. They produce dramatically better
    # tables (correct empty cells, vertical merges) and make figures
    # readable to downstream agents. They require Ollama with a vision
    # model running locally; if Ollama isn't there the user just unticks.
    "vision_tables": True,
    "vision_tables_only": False,
    "describe_figures": True,
    "describe_only": False,
    "redescribe": False,
    "model": "qwen2.5vl:7b",
    "follow_log": True,
    "geometry": "900x720",
}


def load() -> dict[str, Any]:
    """Load settings, falling back to DEFAULTS for any missing keys."""
    data = dict(DEFAULTS)
    try:
        if SETTINGS_PATH.exists():
            with SETTINGS_PATH.open("r", encoding="utf-8") as f:
                data.update(json.load(f))
    except Exception:
        pass
    return data


def save(data: dict[str, Any]) -> None:
    """Persist settings to disk. Best-effort; never raises."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with SETTINGS_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Editable script copy
# ---------------------------------------------------------------------------

def bundled_script_path() -> Path:
    """Return path to the read-only bundled parse_pdf.py.

    Search order:
      1. PyInstaller's _MEIPASS/bundled_script/parse_pdf.py
      2. The repo path .claude/skills/parse-pdf/parse_pdf.py (dev mode)
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / "bundled_script" / "parse_pdf.py"
        if cand.exists():
            return cand
    here = Path(__file__).resolve().parent
    cand = here.parent / ".claude" / "skills" / "parse-pdf" / "parse_pdf.py"
    if cand.exists():
        return cand
    raise FileNotFoundError(
        "Could not locate bundled parse_pdf.py "
        "(checked PyInstaller _MEIPASS and repo path)."
    )


def ensure_script_copy() -> Path:
    """Make sure SCRIPT_COPY_PATH exists; copy from bundled if not.

    Returns the path to the editable script copy.
    """
    if not SCRIPT_COPY_PATH.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled_script_path(), SCRIPT_COPY_PATH)
    return SCRIPT_COPY_PATH


def reset_script_copy() -> Path:
    """Overwrite the editable copy with the bundled original."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundled_script_path(), SCRIPT_COPY_PATH)
    return SCRIPT_COPY_PATH
