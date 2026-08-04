"""
MockKnowledgeProvider — in-memory IKnowledgeProvider, zero I/O. Lets
Conversation Service tests exercise the "context injected" / "no context"
branches without standing up Knowledge Service or Postgres, matching
MockConfigProvider's role for Config SDK consumers.
"""

from __future__ import annotations

from ..models import ChunkSource, RetrievalPolicy, RetrievedChunk, RetrievedContext


class MockKnowledgeProvider:
    def __init__(self) -> None:
        # (tenant_slug, agent_slug) -> list[RetrievedChunk], set via
        # add_chunk(). An agent with no entry here has no eligible KB.
        self._chunks: dict[tuple[str, str], list[RetrievedChunk]] = {}

    def add_chunk(
        self,
        tenant_slug: str,
        agent_slug: str,
        content: str,
        score: float = 0.9,
        **source_kwargs,
    ) -> RetrievedChunk:
        defaults = dict(
            kb_id="kb-1", kb_slug="kb", document_id="doc-1", document_title="Doc",
            chunk_id=f"chunk-{len(self._chunks.get((tenant_slug, agent_slug), []))}",
            page=None, language=None, tags={}, version=1,
        )
        chunk = RetrievedChunk(content=content, score=score, source=ChunkSource(**{**defaults, **source_kwargs}))
        self._chunks.setdefault((tenant_slug, agent_slug), []).append(chunk)
        return chunk

    async def retrieve(
        self,
        tenant_slug: str,
        agent_slug: str,
        query: str,
        policy: RetrievalPolicy | None = None,
    ) -> RetrievedContext | None:
        chunks = self._chunks.get((tenant_slug, agent_slug))
        if not chunks:
            return None
        policy = policy or RetrievalPolicy()
        # A test double has no agent_retrieval_policies row to fall back to
        # — these are the same system-default numbers services/knowledge/
        # retrieval.py's _resolve_policy() bottoms out at, applied only when
        # this test didn't set an explicit override on the policy it passed.
        top_k = policy.top_k if policy.top_k is not None else 5
        minimum_score = policy.minimum_score if policy.minimum_score is not None else 0.0
        include_citations = policy.include_citations if policy.include_citations is not None else True

        selected = sorted(chunks, key=lambda c: c.score, reverse=True)[:top_k]
        selected = [c for c in selected if c.score >= minimum_score]
        if not selected:
            return None
        return RetrievedContext(
            chunks=selected,
            sources=list(dict.fromkeys(c.source.document_title for c in selected)),
            confidence=selected[0].score,
            latency_ms=0.0,
            token_count=sum(len(c.content.split()) for c in selected),
            include_citations=include_citations,
            retrieval_metadata={"mock": True},
        )

    async def close(self) -> None:
        pass  # no real transport to close
