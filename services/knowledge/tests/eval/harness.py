"""Chunking bake-off harness. Indexes a corpus with each candidate strategy using a deterministic hashing embedder
(no Ollama, fully reproducible) and reports recall@5, MRR and answer-in-context rate per strategy and document type.

A hash embedder is a bag-of-hashed-words/character-trigrams model: it rewards lexical overlap and has no semantic
understanding, so these numbers approximate how the strategies compare for lexical-heavy retrieval; they do not
predict the real embedding model's scores. Use them to catch chunking regressions and to rank strategies, then
confirm on the real model and on real tenant questions before choosing a per-type default."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Callable

from services.knowledge.chunker import ChunkConfig, chunk_document
from services.knowledge.chunking import chunk_text
from services.knowledge.parsers.model import TABLE, Block
from services.knowledge.parsers.textblocks import blocks_from_text

DIMS = 768
_WORD = re.compile(r"[^\W_]+", re.UNICODE)  # underscores split words, as a real tokenizer would


def hash_embed(text: str) -> list[float]:
    """Signed feature hashing of lowercase words (weight 1) and character trigrams of each word (weight 0.3)."""
    vec = [0.0] * DIMS
    for word in _WORD.findall(text.lower()):
        grams = [(word, 1.0)] + [(word[i:i + 3], 0.3) for i in range(max(0, len(word) - 2))]
        for gram, weight in grams:
            h = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
            vec[h % DIMS] += weight if (h >> 40) & 1 else -weight
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


@dataclass
class IndexedChunk:
    doc: str
    embed_text: str       # what is embedded (and searched)
    context: str          # what the LLM would be given if this chunk is retrieved (the parent, when there is one)
    vector: list[float] = field(repr=False, default_factory=list)


Strategy = Callable[[str, str, str], list[IndexedChunk]]  # (doc_name, doc_kind, text) -> chunks


def _blocks(kind: str, text: str) -> list[Block]:
    if kind == "table":
        rows = tuple(tuple(r.split(",")) for r in text.splitlines())
        return [Block(TABLE, rows=rows)]
    return blocks_from_text(text)


def fixed_window(name: str, kind: str, text: str) -> list[IndexedChunk]:
    return [IndexedChunk(name, c.content, c.content) for c in chunk_text(text.replace("\n", "\n\n") if kind == "table" else text)]


def recursive_prose(name: str, kind: str, text: str) -> list[IndexedChunk]:
    r = chunk_document(_blocks(kind, text), strategy="prose")
    return [IndexedChunk(name, c.content, c.content) for c in r.children]


def structure_children(name: str, kind: str, text: str) -> list[IndexedChunk]:
    r = chunk_document(_blocks(kind, text), config=ChunkConfig(parent_max=900))
    return [IndexedChunk(name, c.content, c.content) for c in r.children]


def structure_parent_child(name: str, kind: str, text: str) -> list[IndexedChunk]:
    r = chunk_document(_blocks(kind, text), config=ChunkConfig(parent_max=900))
    return [
        IndexedChunk(name, c.content, r.parents[c.parent].content if c.parent is not None else c.content)
        for c in r.children
    ]


STRATEGIES: dict[str, Strategy] = {
    "fixed-window": fixed_window,
    "recursive-prose": recursive_prose,
    "structure": structure_children,
    "structure+parent": structure_parent_child,
}


def _norm(s: str) -> str:
    """Lowercase words only, so markdown pipes, commas and breadcrumbs do not decide whether an answer is present."""
    return " ".join(_WORD.findall(s.lower()))


@dataclass
class Metrics:
    n: int = 0
    recall_at_5: float = 0.0
    mrr: float = 0.0
    answer_in_context: float = 0.0


@dataclass
class Report:
    by_strategy: dict[str, Metrics]
    by_strategy_and_kind: dict[tuple[str, str], Metrics]
    failures: dict[str, list[str]]

    def winner(self, kind: str) -> str:
        return max(STRATEGIES, key=lambda s: (self.by_strategy_and_kind[(s, kind)].mrr, self.by_strategy_and_kind[(s, kind)].recall_at_5))


def _score(index: list[IndexedChunk], questions: list, docs: dict, embed, k: int, into: list[Metrics]) -> list[str]:
    missed: list[str] = []
    for q in questions:
        qv = embed(q.question)
        top = sorted(index, key=lambda c: -_dot(qv, c.vector))[:k]
        answer = _norm(q.answer)
        rank = next((i + 1 for i, c in enumerate(top) if c.doc == q.doc and answer in _norm(c.embed_text)), None)
        context = _norm(" ".join(c.context for c in top[:3]))
        in_context = all(_norm(t) in context for t in q.terms)
        for m in into:
            m.n += 1
            m.recall_at_5 += rank is not None
            m.mrr += 1 / rank if rank else 0.0
            m.answer_in_context += in_context
        if rank is None:
            missed.append(q.question)
    return missed


def _index(docs: dict, strategy: Strategy, embed) -> list[IndexedChunk]:
    out: list[IndexedChunk] = []
    for dname, (kind, text) in docs.items():
        for c in strategy(dname, kind, text):
            c.vector = embed(c.embed_text)
            out.append(c)
    return out


def evaluate(
    docs: dict[str, tuple[str, str]], questions: list, strategies: dict[str, Strategy] | None = None,
    embed: Callable[[str], list[float]] = hash_embed, k: int = 5,
) -> Report:
    """Two views. by_strategy_and_kind: one index per document kind (the strategy is chosen per document type, so
    each kind competes only against its own kind). by_strategy: one mixed index of every document, like a real KB."""
    strategies = strategies or STRATEGIES
    by_s: dict[str, Metrics] = {}
    by_sk: dict[tuple[str, str], Metrics] = {}
    failures: dict[str, list[str]] = {}
    kinds = sorted({kind for kind, _ in docs.values()})
    for sname, strategy in strategies.items():
        by_s[sname] = Metrics()
        failures[sname] = _score(_index(docs, strategy, embed), questions, docs, embed, k, [by_s[sname]])
        for kind in kinds:
            sub = {n: v for n, v in docs.items() if v[0] == kind}
            qs = [q for q in questions if q.doc in sub]
            m = by_sk.setdefault((sname, kind), Metrics())
            _score(_index(sub, strategy, embed), qs, docs, embed, k, [m])
    for m in [*by_s.values(), *by_sk.values()]:
        if m.n:
            m.recall_at_5 /= m.n
            m.mrr /= m.n
            m.answer_in_context /= m.n
    return Report(by_s, by_sk, failures)


def format_report(r: Report) -> str:
    lines = [f"{'strategy':<18}{'kind':<8}{'n':>3}{'recall@5':>10}{'MRR':>8}{'in-ctx':>8}"]
    for (s, kind), m in sorted(r.by_strategy_and_kind.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        lines.append(f"{s:<18}{kind:<8}{m.n:>3}{m.recall_at_5:>10.2f}{m.mrr:>8.2f}{m.answer_in_context:>8.2f}")
    for s, m in r.by_strategy.items():
        lines.append(f"{s:<18}{'mixed KB':<8}{m.n:>3}{m.recall_at_5:>10.2f}{m.mrr:>8.2f}{m.answer_in_context:>8.2f}")
    return "\n".join(lines)
