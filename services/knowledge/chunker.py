"""Chunking strategies and the switch that picks one (KNOWLEDGE_CHUNKER = auto | structure | recursive | fixed).

The default is chosen by the bake-off in tests/eval (see docs/knowledge-chunking.md): a more complex strategy has to beat
the simpler one to be the default, and on the golden set structure-aware and recursive splitting did not beat fixed
windows for headed or plain prose, so `auto` uses fixed windows there. It keeps the shape routes for Q&A pairs and
tables, where fixed windows are wrong by construction (they cut pairs and rows and drop the repeated header).

Routes by document shape (what `structure` mode uses everywhere):
  qa         3+ Q:/A: pairs                      -> one pair per chunk
  table      mostly tables / a CSV               -> row groups, header repeated in every group
  structure  has headings                        -> split on headings, lists and tables kept whole
  prose      no structure                        -> recursive paragraph -> line -> sentence -> word -> window
  fixed      any splitter failure, or the data-chosen default for prose and headed documents -> fixed windows

Every embedded (child) chunk is prefixed with its heading breadcrumb and is held to ChunkConfig.cap estimated tokens by
a hard-split fallback, so none can exceed the embedder's limit. Sections that split into several children also get a
parent row (is_parent, never embedded) holding the whole section; children point at it via Chunk.parent."""

from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field

from .chunking import _BUDGET_S, _DEADLINE, _MAX_CHUNKS, Chunk, ChunkLimitError, chunk_text, tick
from .parsers.model import CODE, HEADING, LIST, PARAGRAPH, TABLE, Block, render_row
from .parsers.textblocks import blocks_from_text
from .tokens import EstimatingTokenCounter, TokenCounter

log = logging.getLogger(__name__)

CHUNKER_VERSIONS = {"structure": "structure-v1", "prose": "recursive-v1", "qa": "qa-v1", "table": "table-v1",
                    "fixed": "fixed-v1"}


MODES = ("auto", "structure", "recursive", "fixed")


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, default))
        return v if v > 0 else default
    except ValueError:
        return default


def chunker_mode() -> str:
    """KNOWLEDGE_CHUNKER, read per call so it can change without a restart. Unknown values fall back to auto."""
    raw = os.environ.get("KNOWLEDGE_CHUNKER", "auto").strip().lower() or "auto"
    if raw not in MODES:
        log.warning("KNOWLEDGE_CHUNKER=%r is not one of %s; using auto", raw, MODES)
        return "auto"
    return raw


def chunker_version(strategy: str) -> str:
    """What is recorded on the document: the strategy that actually produced the chunks, never the one intended."""
    return CHUNKER_VERSIONS.get(strategy, f"{strategy}-v1")
DEFAULT_EMBED_MAX_TOKENS = 512
MIN_EMBED_TOKENS, MAX_EMBED_TOKENS = 64, 8192


@dataclass(frozen=True)
class ChunkConfig:
    cap: int = DEFAULT_EMBED_MAX_TOKENS  # hard max (estimated tokens) of any embedded chunk, breadcrumb included
    child_target: int = 250
    parent_max: int = 900
    min_section: int = 40                # a section smaller than this folds into the next one
    pack_target: int | None = None       # sibling sections are packed up to this many tokens (default: child_target)
    breadcrumb: bool = True              # prefix the heading breadcrumb to the embedded text (section_path is stored either way)
    fixed_words: int = 200               # fixed-window size and overlap, in words (the legacy chunk_text defaults)
    fixed_overlap: int = 40

    @classmethod
    def for_embedder(cls, max_tokens: object) -> "ChunkConfig":
        """`max_tokens` comes from provider_configs.extra, which a tenant admin writes: anything that is not a plain
        int is ignored (default 512) and an int is clamped to a sane range."""
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            max_tokens = DEFAULT_EMBED_MAX_TOKENS
        cap = min(MAX_EMBED_TOKENS, max(MIN_EMBED_TOKENS, max_tokens))
        return cls(cap=cap, child_target=min(250, int(cap * 0.55)))


@dataclass
class ChunkResult:
    children: list[Chunk] = field(default_factory=list)
    parents: list[Chunk] = field(default_factory=list)
    strategy: str = "prose"


# ── splitting to a token budget ─────────────────────────────────────────

_PARAGRAPH = re.compile(r"\n\s*\n")
_LINE = re.compile(r"\n")
_SENTENCE = re.compile(r"(?<=[.!?…।॥؟])\s+|(?<=[。！？])")
_WORD = re.compile(r"\s+")
_SEPARATORS = (_PARAGRAPH, _LINE, _SENTENCE, _WORD)


def _pieces(text: str, sep: re.Pattern) -> list[str]:
    """Split on `sep`, keeping each separator on the end of its piece so ''.join(pieces) == text."""
    out, start = [], 0
    for m in sep.finditer(text):
        if m.end() > start:
            out.append(text[start:m.end()])
            start = m.end()
    if start < len(text):
        out.append(text[start:])
    return out


_COMBINING_LOOKBACK = 16


def _hard_window(text: str, budget: int, counter: TokenCounter) -> list[str]:
    """Last resort: fixed windows sized by the counter, cut at whitespace when one is near and never inside a
    combining sequence. Always terminates and always meets the budget (a window shrinks until it fits)."""
    out: list[str] = []
    n, i = len(text), 0
    est = max(1, counter.count(text))
    size = max(1, int(n * budget / est * 0.9))
    while i < n:
        j = min(n, i + size)
        tick(len(out))
        while j > i + 1 and counter.count(text[i:j]) > budget:
            j = i + max(1, int((j - i) * 0.8))
        if j < n:
            back = text.rfind(" ", i + (j - i) * 3 // 4, j)
            if back > i:
                j = back + 1
            # Do not cut inside a combining sequence, but only look back a few characters: a run of marks longer
            # than that (a hostile file, or Zalgo text) is cut where the window ends. Backing off to one character
            # per chunk is how 100 KB became 49,602 chunks.
            cut = j
            while j > i + 1 and j < n and unicodedata.category(text[j]).startswith("M") and cut - j < _COMBINING_LOOKBACK:
                j -= 1
            if j < n and unicodedata.category(text[j]).startswith("M"):
                j = cut
        out.append(text[i:j])
        i = j
    return out


def split_to_budget(text: str, budget: int, counter: TokenCounter, level: int = 0) -> list[str]:
    """Pieces of `text`, each within `budget` tokens: paragraphs, then lines, sentences, words, fixed windows;
    neighbours are re-packed greedily so pieces stay as large as the budget allows."""
    if counter.count(text) <= budget:
        return [text]
    if level >= len(_SEPARATORS):
        return _hard_window(text, budget, counter)
    parts = _pieces(text, _SEPARATORS[level])
    if len(parts) <= 1:
        return split_to_budget(text, budget, counter, level + 1)
    out: list[str] = []
    current = ""
    used = 0  # running estimate: parts are cut at whitespace, so counts add up (each part's own count is rounded up)
    for part in parts:
        tick(len(out))
        size = counter.count(part)
        if current and used + size > budget:
            out.append(current)
            current, used = "", 0
        if size > budget:
            if current:
                out.append(current)
                current, used = "", 0
            out.extend(split_to_budget(part, budget, counter, level + 1))
        else:
            current += part
            used += size
    if current:
        out.append(current)
    return out


# ── sections ────────────────────────────────────────────────────────────

@dataclass
class _Section:
    path: tuple[str, ...]
    blocks: list[Block]


def _sections(blocks: list[Block]) -> list[_Section]:
    out: list[_Section] = []
    stack: list[tuple[int, str]] = []
    current = _Section((), [])
    for b in blocks:
        if b.kind == HEADING:
            if current.blocks:
                out.append(current)
            while stack and stack[-1][0] >= b.level:
                stack.pop()
            stack.append((b.level, b.text.strip()))
            current = _Section(tuple(t for _, t in stack), [])
        else:
            current.blocks.append(b)
    if current.blocks:
        out.append(current)
    return out


def _fold_small(sections: list[_Section], counter: TokenCounter, minimum: int) -> list[_Section]:
    """A tiny section (a heading and a line) becomes the lead of the next one, its heading kept inline."""
    out: list[_Section] = []
    carry: list[Block] = []
    for i, sec in enumerate(sections):
        lead, carry = carry, []
        blocks = lead + sec.blocks
        if sum(counter.count(b.render()) for b in blocks) < minimum and i + 1 < len(sections):
            heading = [Block(PARAGRAPH, sec.path[-1])] if sec.path else []
            carry = lead + heading + sec.blocks
            continue
        out.append(_Section(sec.path, blocks))
    return out


def _merge_siblings(sections: list[_Section], counter: TokenCounter, target: int) -> list[_Section]:
    """Adjacent sibling sections (same parent heading) are packed together up to `target` tokens, each heading kept
    inline, so short sections do not become tiny chunks. The merged chunk's breadcrumb is the shared parent path."""
    out: list[_Section] = []
    group: list[_Section] = []
    used = 0

    def flush() -> None:
        if len(group) == 1:
            out.append(group[0])
        elif group:
            blocks: list[Block] = []
            for sec in group:
                blocks.append(Block(HEADING, sec.path[-1], level=len(sec.path)))
                blocks.extend(sec.blocks)
            out.append(_Section(group[0].path[:-1], blocks))
        group.clear()

    for sec in sections:
        size = sum(counter.count(b.render()) for b in sec.blocks) + counter.count(sec.path[-1] if sec.path else "")
        # A section with no heading (text before the first heading) is never merged: it has no heading to keep inline.
        same_parent = group and sec.path and group[0].path and group[0].path[:-1] == sec.path[:-1]
        if group and (not same_parent or used + size > target):
            flush()
            used = 0
        group.append(sec)
        used += size
    flush()
    return out


# ── children ────────────────────────────────────────────────────────────

def _crumb(path: tuple[str, ...], counter: TokenCounter, cap: int) -> str:
    parts = list(path)
    while len(parts) > 1 and counter.count(" > ".join(parts)) > cap // 4:
        parts.pop(0)
    crumb = " > ".join(parts)
    if counter.count(crumb) > cap // 4:
        crumb = split_to_budget(crumb, cap // 4, counter)[0].strip()
    return crumb


@dataclass
class _Piece:
    body: str
    page: int | None
    whole: bool = False  # a list/table: never merged into a neighbour's chunk if that would split it


def _table_groups(rows: tuple[tuple[str, ...], ...], budget: int, counter: TokenCounter) -> list[str]:
    """Row groups with the header row repeated in every group."""
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    head = render_row(header)
    if not body:
        return [head]
    groups: list[str] = []
    current: list[str] = []
    used = counter.count(head)
    for row in body:
        tick(len(groups))
        line = render_row(row)
        cost = counter.count(line) + 1
        if current and used + cost > budget:
            groups.append(head + "\n" + "\n".join(current))
            current, used = [], counter.count(head)
        current.append(line)
        used += cost
    if current:
        groups.append(head + "\n" + "\n".join(current))
    return groups


def _pieces_for(blocks: list[Block], budget: int, whole_limit: int, counter: TokenCounter) -> list[_Piece]:
    """Blocks to pieces. Paragraphs split down to `budget`; a list or table stays one piece while it fits
    `whole_limit` and otherwise splits (list items / table row groups with the header repeated) at `budget`."""
    out: list[_Piece] = []
    for b in blocks:
        tick()
        text = b.render().strip()
        if not text:
            continue
        if b.kind in (TABLE, LIST) and counter.count(text) <= whole_limit:
            out.append(_Piece(text, b.page, whole=True))
        elif b.kind == TABLE:
            out.extend(_Piece(g, b.page, whole=True) for g in _table_groups(b.rows, budget, counter))
        else:
            out.extend(_Piece(p.strip(), b.page) for p in split_to_budget(text, budget, counter) if p.strip())
    return out


def _pack(pieces: list[_Piece], budget: int, counter: TokenCounter) -> list[tuple[str, int | None]]:
    """Greedy packing of pieces into bodies of <= budget tokens; a whole piece stays in one body."""
    out: list[tuple[str, int | None]] = []
    cur: list[str] = []
    page: int | None = None
    used = 0
    for p in pieces:
        cost = counter.count(p.body)
        if cur and used + cost + 2 > budget:
            out.append(("\n\n".join(cur), page))
            cur, used, page = [], 0, None
        if not cur:
            page = p.page
        cur.append(p.body)
        used += cost + 2
    if cur:
        out.append(("\n\n".join(cur), page))
    return out


def _with_crumb(crumb: str, body: str) -> str:
    return f"{crumb}\n\n{body}" if crumb else body


def _build_sections(
    sections: list[_Section], counter: TokenCounter, cfg: ChunkConfig, parents: bool,
) -> ChunkResult:
    res = ChunkResult()
    for sec in sections:
        tick(len(res.children))
        crumb = _crumb(sec.path, counter, cfg.cap) if cfg.breadcrumb else ""
        path = " > ".join(sec.path) or None
        prefix = counter.count(crumb) + 2 if crumb else 0
        budget = max(24, min(cfg.child_target, cfg.cap - 4) - prefix)  # packing aims for child_target in all
        hard = max(24, cfg.cap - prefix - 4)                           # a whole table/list may use up to the cap
        bodies = _pack(_pieces_for(sec.blocks, budget, hard, counter), budget, counter)
        first = len(res.children)
        for body, page in bodies:
            tick(len(res.children))
            content = _with_crumb(crumb, body)
            if counter.count(content) > cfg.cap:  # a whole table/list that overshoots once the breadcrumb is added
                for sub in split_to_budget(body, hard, counter):
                    res.children.append(_child(_with_crumb(crumb, sub.strip()), path, page, counter))
            else:
                res.children.append(_child(content, path, page, counter))
        if parents and len(res.children) - first > 1:
            _add_parents(res, first, crumb, path, counter, cfg)
    return res


def _child(content: str, path: str | None, page: int | None, counter: TokenCounter) -> Chunk:
    return Chunk(content=content, token_count=counter.count(content), section_path=path, page=page)


def _add_parents(
    res: ChunkResult, first: int, crumb: str, path: str | None, counter: TokenCounter, cfg: ChunkConfig,
) -> None:
    """Group a section's consecutive children into parents of <= parent_max tokens; a lone child needs none."""
    strip = len(crumb) + 2 if crumb else 0
    groups: list[list[int]] = [[]]
    used = 0
    for idx in range(first, len(res.children)):
        cost = res.children[idx].token_count
        if groups[-1] and used + cost > cfg.parent_max:
            groups.append([])
            used = 0
        groups[-1].append(idx)
        used += cost
    for g in groups:
        if len(g) < 2:
            continue
        body = "\n\n".join(res.children[i].content[strip:] for i in g)
        content = _with_crumb(crumb, body)
        parent = Chunk(content=content, token_count=counter.count(content), section_path=path,
                       page=res.children[g[0]].page, is_parent=True)
        res.parents.append(parent)
        for i in g:
            c = res.children[i]
            res.children[i] = Chunk(c.content, c.token_count, c.section_path, c.page, parent=len(res.parents) - 1)


# ── Q&A ─────────────────────────────────────────────────────────────────

_Q_RE = re.compile(r"^\s*(?:Q|Question)\s*[:.)\-]\s*(.*)$", re.IGNORECASE)
_A_RE = re.compile(r"^\s*(?:A|Answer)\s*[:.)\-]\s*(.*)$", re.IGNORECASE)


def _qa_events(blocks: list[Block]) -> list[tuple[str, str, int, int | None]]:
    ev: list[tuple[str, str, int, int | None]] = []
    for b in blocks:
        if b.kind == HEADING:
            ev.append(("h", b.text.strip(), b.level, b.page))
        else:
            for ln in b.render().split("\n"):
                if ln.strip():
                    ev.append(("l", ln.rstrip(), 0, b.page))
    return ev


def _count_qa(blocks: list[Block]) -> int:
    return sum(1 for kind, text, _, _ in _qa_events(blocks) if kind == "l" and _Q_RE.match(text))


def _build_qa(blocks: list[Block], counter: TokenCounter, cfg: ChunkConfig) -> ChunkResult:
    res = ChunkResult(strategy="qa")
    stack: list[tuple[int, str]] = []
    pair: list[str] = []
    pair_page: int | None = None
    pair_path: str | None = None

    def flush() -> None:
        nonlocal pair, pair_page
        if not pair:
            return
        question = pair[0]
        rest = "\n".join(pair[1:])
        crumb = pair_path or ""
        for part in split_to_budget(rest, max(24, cfg.cap - counter.count(question) - counter.count(crumb) - 8), counter) or [""]:
            body = question + ("\n" + part.strip() if part.strip() else "")
            content = _with_crumb(crumb, body)
            if counter.count(content) > cfg.cap:  # an enormous question: windows of the whole thing
                for sub in split_to_budget(content, cfg.cap, counter):
                    res.children.append(_child(sub.strip(), pair_path, pair_page, counter))
            else:
                res.children.append(_child(content, pair_path, pair_page, counter))
        pair, pair_page = [], None

    for kind, text, level, page in _qa_events(blocks):
        tick(len(res.children))
        if kind == "h":
            flush()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            continue
        if _Q_RE.match(text):
            flush()
            pair_path = " > ".join(t for _, t in stack) or None
            pair_page = page
        elif not pair:  # lead-in text before the first question
            pair_path = " > ".join(t for _, t in stack) or None
            pair_page = page
            pair = [text]
            continue
        pair.append(text)
    flush()
    return res


# ── routing and entry points ────────────────────────────────────────────

def route(blocks: list[Block]) -> str:
    if _count_qa(blocks) >= 3:
        return "qa"
    total = sum(len(b.render()) for b in blocks) or 1
    if sum(len(b.render()) for b in blocks if b.kind == TABLE) / total >= 0.6:
        return "table"
    if any(b.kind == HEADING for b in blocks):
        return "structure"
    return "prose"


def _fixed(blocks: list[Block], language: str | None, counter: TokenCounter, cfg: ChunkConfig) -> ChunkResult:
    text = "\n\n".join(b.render() for b in blocks if b.render().strip())
    res = ChunkResult(strategy="fixed")
    # ~2.5 estimated tokens per word: a small embedder cap shrinks the window, a large one keeps the legacy 200 words.
    size = max(20, min(cfg.fixed_words, int(cfg.cap / 2.5)))
    overlap = min(cfg.fixed_overlap, size // 4)
    for c in chunk_text(text, chunk_size=size, chunk_overlap=overlap, language=language):
        for piece in split_to_budget(c.content, cfg.cap, counter):
            if piece.strip():
                tick(len(res.children))
                res.children.append(Chunk(piece.strip(), counter.count(piece)))
    return res


def pick_strategy(blocks: list[Block], mode: str) -> str:
    """The strategy a mode uses for this document. Q&A and table shapes keep their routes in every mode but `fixed`."""
    shape = route(blocks)
    if mode == "fixed":
        return "fixed"
    if shape in ("qa", "table") or mode == "structure":
        return shape
    return "prose" if mode == "recursive" else "fixed"  # auto: fixed windows won the sweep for headed and plain prose


def chunk_document(
    blocks: list[Block], language: str | None = None, *, counter: TokenCounter | None = None,
    config: ChunkConfig | None = None, strategy: str | None = None,
) -> ChunkResult:
    """Children (embedded) and parents (context) for a parsed document. `strategy` forces a route (the bake-off
    uses it); otherwise KNOWLEDGE_CHUNKER and the document's shape pick one. Any splitter error falls back to fixed
    windows, and the result's `strategy` is always the one that actually ran."""
    budget_s, max_chunks = _env_float("KNOWLEDGE_CHUNK_BUDGET_S", 60.0), int(_env_float("KNOWLEDGE_MAX_CHUNKS_PER_DOC", 5000))
    tokens = (_DEADLINE.set(time.monotonic() + budget_s), _BUDGET_S.set(budget_s), _MAX_CHUNKS.set(max_chunks))
    try:
        return _chunk_document(blocks, language, counter, config, strategy)
    finally:
        _DEADLINE.reset(tokens[0])
        _BUDGET_S.reset(tokens[1])
        _MAX_CHUNKS.reset(tokens[2])


def _chunk_document(
    blocks: list[Block], language: str | None, counter: TokenCounter | None, config: ChunkConfig | None,
    strategy: str | None,
) -> ChunkResult:
    cfg = config or ChunkConfig()
    counter = counter or EstimatingTokenCounter(language=language)
    blocks = [b for b in blocks if b.render().strip()]
    if not blocks:
        return ChunkResult()
    if all(b.kind == HEADING for b in blocks):  # nothing under any heading: the headings are the content
        blocks = [Block(PARAGRAPH, b.text, page=b.page) for b in blocks]
    chosen = strategy or pick_strategy(blocks, chunker_mode())
    try:
        if chosen == "qa":
            res = _build_qa(blocks, counter, cfg)
        elif chosen == "fixed":
            res = _fixed(blocks, language, counter, cfg)
        else:
            if chosen == "prose":
                sections = [_Section((), blocks)]
            elif chosen == "structure":
                sections = _merge_siblings(
                    _fold_small(_sections(blocks), counter, cfg.min_section), counter, cfg.pack_target or cfg.child_target,
                )
            else:
                sections = _sections(blocks) or [_Section((), blocks)]
            res = _build_sections(sections, counter, cfg, parents=chosen != "table")
            res.strategy = chosen
        if not res.children:
            raise ValueError("splitter produced no chunks")
        tick(len(res.children))
        _check(res, counter, cfg)
    except ChunkLimitError:
        raise  # a limit is final: the fixed-window fallback would only hit the same one
    except Exception:
        log.warning("chunking strategy %r failed; falling back to fixed windows", chosen, exc_info=True)
        res = _fixed(blocks, language, counter, cfg)
    return res


def _check(res: ChunkResult, counter: TokenCounter, cfg: ChunkConfig) -> None:
    for c in res.children:
        if not c.content.strip() or counter.count(c.content) > cfg.cap:
            raise ValueError("chunk empty or over the token cap")


def chunk_plain_text(text: str, language: str | None = None, **kw) -> ChunkResult:
    """Convenience for callers that only have text: parse its markdown-ish structure, then chunk_document."""
    return chunk_document(blocks_from_text(text), language, **kw)
