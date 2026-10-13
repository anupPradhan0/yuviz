"""Token counting for chunk sizing. The embedder's real tokenizer is not available offline (and differs per model),
so sizing runs on a pluggable TokenCounter whose default is a deliberately conservative estimate: it must never
under-count, because an under-count is how a chunk silently overflows the embedder and gets truncated."""

from __future__ import annotations

import math
import re
from typing import Protocol

from .chunking import count_tokens


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


# Used as `len(text) - len(RE.sub("", text))`: C-speed class counts with no per-character Python loop.
_LETTER = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"[0-9]")
_SPACE = re.compile(r"\s")
_LEGACY_PROXY_MAX_CHARS = 50_000


def _count(regex: re.Pattern, text: str) -> int:
    return len(text) - len(regex.sub("", text))


class EstimatingTokenCounter:
    """Per-character estimate: ASCII letters ~0.3 token (wordpiece averages ~0.25), digits 0.5, whitespace 0, and
    everything else 1.0 (punctuation, symbols, and every non-ASCII character: wordpiece vocabularies cover Hindi,
    Arabic, Thai, Chinese and combining marks poorly and fall back to roughly one token per character), times a
    safety margin. Never lower than the platform's legacy proxy (count_tokens: words, or characters for unspaced
    scripts) on text short enough to run it on."""

    def __init__(self, language: str | None = None, margin: float = 1.15) -> None:
        self.language = language
        self.margin = margin

    def count(self, text: str) -> int:
        if not text:
            return 0
        letters = _count(_LETTER, text)
        digits = _count(_DIGIT, text)
        other = len(text) - letters - digits - _count(_SPACE, text)
        estimate = math.ceil((0.3 * letters + 0.5 * digits + other) * self.margin)
        if len(text) <= _LEGACY_PROXY_MAX_CHARS:
            estimate = max(estimate, count_tokens(text, self.language))
        return estimate
