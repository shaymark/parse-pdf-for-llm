"""Query a local Ollama daemon for available vision models.

Best-effort: a 1s timeout, swallows all errors. Returns DEFAULTS when
Ollama isn't running.
"""
from __future__ import annotations

import json
import urllib.request

OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
DEFAULT_MODELS = ["qwen2.5vl:7b"]


def list_models(timeout: float = 1.0) -> list[str]:
    """Return a sorted list of locally installed Ollama model tags.

    Falls back to DEFAULT_MODELS if Ollama is unreachable.
    """
    try:
        with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = sorted({m.get("name", "") for m in data.get("models", []) if m.get("name")})
        if not models:
            return list(DEFAULT_MODELS)
        # Surface known-good vision models first if present.
        preferred = [m for m in models if "vl" in m.lower() or "vision" in m.lower()]
        rest = [m for m in models if m not in preferred]
        return preferred + rest
    except Exception:
        return list(DEFAULT_MODELS)
