"""A deterministic cross-encoder-like scorer for the golden-set harness. It reads the query and the chunk TOGETHER
(word overlap plus adjacent-word-pair overlap, normalised by chunk length), which is the one thing a bi-encoder
embedder cannot do, so it can reorder what the embedder ranked. It is NOT a quality claim: it proves that the rerank
plumbing (flag, hook, ordering, latency accounting) works end to end. Whether a real reranker improves a tenant's
retrieval must be measured with that provider and real tenant questions (docs/knowledge-phase7.md)."""

from __future__ import annotations

import asyncio
import re

from services.knowledge.vector_repository import VectorMatch, query_terms

_WORD = re.compile(r"\w+", re.UNICODE)


def _pairs(words: list[str]) -> set[tuple[str, str]]:
    return set(zip(words, words[1:]))


def cross_score(query: str, text: str) -> float:
    q_terms = query_terms(query)
    words = _WORD.findall(text.casefold())
    if not q_terms or not words:
        return 0.0
    vocab = set(words)
    overlap = sum(t in vocab for t in q_terms) / len(q_terms)
    q_words = _WORD.findall(query.casefold())
    pair_hits = len(_pairs(q_words) & _pairs(words)) / max(len(_pairs(q_words)), 1)
    return overlap + 0.5 * pair_hits - 0.0005 * len(words) ** 0.5  # a slight preference for tighter chunks


class FakeCrossEncoderReranker:
    configured = True

    def __init__(self, latency_s: float = 0.0) -> None:
        self.latency_s = latency_s
        self.calls = 0

    async def rerank(self, query: str, matches: list[VectorMatch]) -> list[VectorMatch]:
        self.calls += 1
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        order = sorted(range(len(matches)), key=lambda i: (-cross_score(query, matches[i].content), i))
        return [matches[i] for i in order]
