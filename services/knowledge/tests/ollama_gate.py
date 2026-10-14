"""Skip marker for the few tests that embed with a real Ollama model (nomic-embed-text) instead of the fake embedder.
They stay runnable on a machine that has Ollama (set OLLAMA_URL if it is not on localhost:11434) and skip, instead
of failing, on one that does not, such as CI."""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

MODEL = "nomic-embed-text"


def _model_available() -> bool:
    base = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=1.0) as resp:
            names = [m.get("name", "") for m in json.load(resp).get("models", [])]
    except Exception:  # noqa: BLE001 - unreachable, refused, malformed: all mean "not available"
        return False
    return any(n == MODEL or n.startswith(f"{MODEL}:") for n in names)


requires_ollama_embeddings = pytest.mark.skipif(
    not _model_available(), reason=f"needs a local Ollama serving the {MODEL} embedding model",
)
