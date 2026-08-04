"""
CacheAsideKnowledgeProvider — the production IKnowledgeProvider. Composes an
availability repository (Redis, cheap boolean) and a retrieval repository
(HTTP, the real vector search) — never constructs a redis-py or httpx client
itself, matching Config SDK's CacheAsideConfigProvider composition pattern.

retrieve() is the exactly-one-call contract Conversation Service depends on:
  1. Redis boolean check (fast path for the common "no KB attached" case —
     zero HTTP round trips, zero added latency for non-RAG agents).
  2. On a Redis miss (unknown, not "false"), ask the HTTP repository's own
     has_enabled_kb — this is the one place a miss costs an extra round
     trip, and only on a cold cache.
  3. If enabled, call the HTTP repository's retrieve() (the one real vector
     search this whole call performs) and map the raw dict into
     RetrievedContext.
Any RepositoryUnavailableError from either repository is caught here and
turned into None — a retrieval-plane outage degrades to "no context
injected", never a failed turn.
"""

from __future__ import annotations

import logging
import time

from ..exceptions import RepositoryUnavailableError
from ..interfaces import IKnowledgeAvailabilityRepository, IRetrievalRepository
from ..models import ChunkSource, RetrievalPolicy, RetrievedChunk, RetrievedContext

log = logging.getLogger(__name__)


class CacheAsideKnowledgeProvider:
    def __init__(
        self,
        availability_repo: IKnowledgeAvailabilityRepository,
        retrieval_repo: IRetrievalRepository,
    ) -> None:
        self._availability_repo = availability_repo
        self._retrieval_repo = retrieval_repo

    async def close(self) -> None:
        await self._availability_repo.close()
        await self._retrieval_repo.close()

    async def retrieve(
        self,
        tenant_slug: str,
        agent_slug: str,
        query: str,
        policy: RetrievalPolicy | None = None,
    ) -> RetrievedContext | None:
        start = time.monotonic()
        # An all-None RetrievalPolicy() (every field defaults to None) is
        # exactly "no per-call override" — it's what gets sent through to
        # Knowledge Service either way, which resolves the effective values
        # itself (see services/knowledge/retrieval.py's _resolve_policy()).
        # This provider never substitutes its own hardcoded numbers.
        policy = policy or RetrievalPolicy()

        try:
            enabled = await self._availability_repo.has_enabled_kb(tenant_slug, agent_slug)
            if enabled is None:  # cache miss — ask the source of truth once
                enabled = await self._retrieval_repo.has_enabled_kb(tenant_slug, agent_slug)
            if not enabled:
                return None

            raw = await self._retrieval_repo.retrieve(tenant_slug, agent_slug, query, policy)
        except RepositoryUnavailableError:
            log.warning(
                "CacheAsideKnowledgeProvider: retrieval plane unavailable, "
                "degrading to no context tenant=%s agent=%s", tenant_slug, agent_slug,
            )
            return None

        if raw is None:
            return None

        chunks = [
            RetrievedChunk(
                content=c["content"],
                score=c["score"],
                source=ChunkSource(
                    kb_id=c["source"]["kb_id"],
                    kb_slug=c["source"]["kb_slug"],
                    document_id=c["source"]["document_id"],
                    document_title=c["source"]["document_title"],
                    chunk_id=c["source"]["chunk_id"],
                    page=c["source"].get("page"),
                    language=c["source"].get("language"),
                    tags=c["source"].get("tags", {}),
                    version=c["source"].get("version", 1),
                ),
            )
            for c in raw["chunks"]
        ]

        return RetrievedContext(
            chunks=chunks,
            sources=raw["sources"],
            confidence=chunks[0].score if chunks else 0.0,
            latency_ms=(time.monotonic() - start) * 1000,
            token_count=raw["token_count"],
            include_citations=raw.get("include_citations", True),
            retrieval_metadata=raw.get("retrieval_metadata", {}),
        )
