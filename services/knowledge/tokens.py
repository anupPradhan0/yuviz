"""Token counting for chunk sizing. The embedder's real tokenizer is not available offline (and differs per model),
so sizing runs on a pluggable TokenCounter whose default is a deliberately conservative estimate: it must never
under-count, because an under-count is how a chunk silently overflows the embedder and gets truncated."""

from __future__ import annotations

import math
import re
from typing import Protocol



class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


# Used as `len(text) - len(RE.sub("", text))`: C-speed class counts with no per-character Python loop.
_LETTER = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"[0-9]")
_SPACE = re.compile(r"\s")
_WORD_FLOOR_MAX_CHARS = 50_000   # the floor only matters for short text; skipping it on long text only lowers the whole, never a part


def _count(regex: re.Pattern, text: str) -> int:
    return len(text) - len(regex.sub("", text))


class EstimatingTokenCounter:
    """Per-character estimate: ASCII letters ~0.3 token (wordpiece averages ~0.25), digits 0.5, whitespace 0, and
    everything else 1.0 (punctuation, symbols, and every non-ASCII character: wordpiece vocabularies cover Hindi,
    Arabic, Thai, Chinese and combining marks poorly and fall back to roughly one token per character), times a
    safety margin, and never lower than the word count (a floor that adds up exactly over whitespace-separated pieces).
    The estimate is additive over pieces cut at whitespace (each piece's count is rounded up), which is what lets the
    splitter keep a running sum as an upper bound. It deliberately does NOT use count_tokens(): that sniffs the script
    of the whole text, so a mixed Latin/CJK text was counted per character as a whole (778) but per word in its
    Latin parts, and chunks were stored over the cap."""

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
        if len(text) <= _WORD_FLOOR_MAX_CHARS:
            estimate = max(estimate, len(text.split()))
        return estimate
