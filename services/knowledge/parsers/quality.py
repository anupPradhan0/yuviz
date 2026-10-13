"""Quality gate run on every parser's output. A hard failure (no text, garbled text) drops to the next
fallback; a soft one (some pages empty, very thin pages) keeps the result but marks it degraded."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

MIN_CHARS_PER_PAGE = 20
_GARBAGE_HARD_RATIO = 0.15
_BAD_CATEGORIES = frozenset({"Cc", "Co", "Cs", "Cn"})
_OK_CONTROLS = frozenset("\t\n\r\f")


@dataclass
class Quality:
    score: float
    chars: int
    garbage_ratio: float
    empty_page_ratio: float
    chars_per_page: float | None
    hard_fail: str | None = None
    soft_issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.hard_fail is None and not self.soft_issues


def garbage_ratio(text: str) -> float:
    """Share of non-space characters that are replacement/control/private-use/unassigned code points."""
    total = bad = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        if ch == "�" or (ch not in _OK_CONTROLS and unicodedata.category(ch) in _BAD_CATEGORIES):
            bad += 1
    return bad / total if total else 0.0


def assess(text: str, pages: list[str] | None = None) -> Quality:
    stripped = text.strip()
    chars = len(stripped)
    garbage = garbage_ratio(stripped)
    empty_ratio = 0.0
    per_page: float | None = None
    soft: list[str] = []
    if pages:
        empty = sum(1 for p in pages if len(p.strip()) < MIN_CHARS_PER_PAGE)
        empty_ratio = empty / len(pages)
        per_page = sum(len(p.strip()) for p in pages) / len(pages)
        if empty_ratio > 0:
            soft.append(f"{empty} of {len(pages)} pages had no readable text")
    hard = None
    if chars == 0:
        hard = "no text could be extracted"
    elif garbage > _GARBAGE_HARD_RATIO:
        hard = "extracted text is garbled"
    score = (1 - min(1.0, garbage * 3)) * (1 - 0.7 * empty_ratio) if hard is None else 0.0
    return Quality(round(score, 3), chars, round(garbage, 4), round(empty_ratio, 3), per_page, hard, soft)
