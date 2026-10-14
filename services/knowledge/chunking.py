"""Fixed-window chunking: paragraph-first ~chunk_size-word windows with chunk_overlap words of overlap.
Word count is the token-count proxy throughout. The structure-aware chunker (chunker.py) is the ingestion default;
this is its last-resort fallback and the behaviour existing callers of chunk_text() rely on.

Languages written without spaces between words (Chinese, Japanese, Thai, Lao, Khmer,
Myanmar) have no words to count, so they are sized by characters instead: token
counts are non-space characters, and windows are _UNSPACED_CHARS_PER_WORD x larger.
"""

from __future__ import annotations

import re
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Sequence

from libs.config_sdk.languages import normalize_language

_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_WHITESPACE_RE = re.compile(r"\s+")

# ISO 639-1 codes of languages written without inter-word spaces.
_UNSPACED_LANGUAGES = frozenset({"zh", "ja", "th", "lo", "km", "my"})
# Han, Hiragana/Katakana, Thai, Lao, Myanmar, Khmer, CJK punctuation, full-width forms.
_UNSPACED_SCRIPT_RE = re.compile(
    r"[一-鿿㐀-䶿぀-ヿ฀-๿຀-໿"
    r"က-႟ក-៿　-〿＀-￯]"
)
# Text is treated as unspaced when more than this share of its non-space chars are.
_UNSPACED_SHARE = 0.30
# Roughly one word's worth of characters in unspaced scripts (token-count proxy).
_UNSPACED_CHARS_PER_WORD = 1.5


@dataclass(frozen=True)
class Chunk:
    content: str
    token_count: int
    # Set by the structure-aware chunker (chunker.py); the fixed-window path here leaves them at their defaults.
    section_path: str | None = None
    page: int | None = None
    parent: int | None = None   # index into the parents list of the ChunkResult this child belongs to
    is_parent: bool = False


def is_unspaced(text: str, language: str | None = None) -> bool:
    """The document's language decides when known; otherwise sniff the script."""
    lang = normalize_language(language)
    if lang is not None:
        return lang in _UNSPACED_LANGUAGES
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return False
    return sum(1 for c in chars if _UNSPACED_SCRIPT_RE.match(c)) / len(chars) > _UNSPACED_SHARE


def count_tokens(text: str, language: str | None = None) -> int:
    """Token-count proxy: words, or non-space characters for unspaced scripts."""
    if is_unspaced(text, language):
        return sum(1 for c in text if not c.isspace())
    return len(text.split())


class ChunkLimitError(ValueError):
    """The document cannot be chunked within the platform's limits (too many chunks, or too much work). A ValueError,
    so the worker treats it as permanent: retrying the same bytes would hit the same limit."""


# Per-document ceilings, set by chunker.chunk_document for the duration of one call (a context variable, so they follow
# the work into asyncio.to_thread and never leak between documents).
_DEADLINE: ContextVar[float | None] = ContextVar("chunk_deadline", default=None)
_MAX_CHUNKS: ContextVar[int | None] = ContextVar("chunk_max", default=None)
_BUDGET_S: ContextVar[float | None] = ContextVar("chunk_budget_s", default=None)


def tick(produced: int = 0) -> None:
    """Cooperative checkpoint for every loop that can run long: raises once the wall-clock budget or the per-document
    chunk ceiling is exceeded. `produced` is how many chunks the caller has built so far."""
    deadline = _DEADLINE.get()
    if deadline is not None and time.monotonic() > deadline:
        raise ChunkLimitError(f"document too complex to index (chunking exceeded its {_BUDGET_S.get():g}s budget)")
    cap = _MAX_CHUNKS.get()
    if cap is not None and produced > cap:
        raise ChunkLimitError(f"document too large to index (more than {cap} chunks)")


def chunk_text(
    text: str, chunk_size: int = 200, chunk_overlap: int = 40, language: str | None = None,
) -> list[Chunk]:
    paragraphs = [p.strip() for p in _PARAGRAPH_RE.split(text) if p.strip()]
    if not paragraphs:
        return []
    if is_unspaced(text, language):
        # Whitespace is kept (collapsed): Thai and Lao use spaces between phrases. Strings, not lists of characters:
        # slicing a str is cheap and a 10 MB document does not become millions of one-character objects.
        return _chunk_units(
            [_WHITESPACE_RE.sub(" ", p) for p in paragraphs],
            int(chunk_size * _UNSPACED_CHARS_PER_WORD), int(chunk_overlap * _UNSPACED_CHARS_PER_WORD),
            joiner="", count=lambda units: sum(1 for c in units if not c.isspace()),
        )
    return _chunk_units([p.split() for p in paragraphs], chunk_size, chunk_overlap, joiner=" ", count=len)


def _chunk_units(
    paragraphs: list, chunk_size: int, chunk_overlap: int, joiner: str, count: Callable[[Sequence[str]], int],
) -> list[Chunk]:
    """Windows of `chunk_size` units with `chunk_overlap` of overlap. `paragraphs` are lists of words or strings of
    characters. Linear: a long paragraph is walked with an index, never re-sliced."""
    def emit(units) -> None:
        content = joiner.join(units).strip() if not isinstance(units, str) else units.strip()
        if content:
            chunks.append(Chunk(content=content, token_count=count(units)))

    if chunk_size < 1 or chunk_overlap < 0 or chunk_overlap >= chunk_size:
        # With overlap >= size the window never advances and the loop below appends chunks until memory runs out.
        raise ValueError(f"invalid chunking window: chunk_size={chunk_size}, chunk_overlap={chunk_overlap}")
    step = chunk_size - chunk_overlap  # every emitted window consumes at least this many fresh units
    chunks: list[Chunk] = []
    current = paragraphs[0][:0]

    for units in paragraphs:
        tick(len(chunks))
        if len(current) and len(current) + len(units) > chunk_size:
            emit(current)
            current = (current[-chunk_overlap:] if chunk_overlap else current[:0]) + units
        else:
            current += units

        # A single paragraph longer than chunk_size on its own still needs splitting, or it would produce one
        # oversized chunk forever.
        pos = 0
        while len(current) - pos > chunk_size:
            tick(len(chunks))
            emit(current[pos:pos + chunk_size])
            pos += step
        if pos:
            current = current[pos:]

    if len(current):
        emit(current)

    return chunks
