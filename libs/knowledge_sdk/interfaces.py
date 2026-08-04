"""
Two narrow protocols, not one unifying transport interface — Redis and HTTP
play genuinely different roles here (unlike Config SDK's two repositories,
which both fetch the same-shaped data). Redis only ever answers the cheap
"does this agent have any enabled knowledge base" pre-check; the actual
query-dependent retrieval always goes to Knowledge Service over HTTP (a
vector search is not a key-value cache-aside read).

IKnowledgeProvider — business-level, the ONLY thing Conversation Service (or
any future consumer) depends on. Exactly one method matters on the call
path: retrieve(). CacheAsideKnowledgeProvider is the production
implementation; MockKnowledgeProvider is the zero-I/O test double.
"""

from __future__ import annotations

from typing import Any, Protocol

from .models import RetrievalPolicy, RetrievedContext


class IKnowledgeProvider(Protocol):
    async def retrieve(
        self,
        tenant_slug: str,
        agent_slug: str,
        query: str,
        policy: RetrievalPolicy | None = None,
    ) -> RetrievedContext | None:
        """Returns None when the agent has no eligible enabled knowledge
        base, or when the retrieval-plane is unavailable — both are a
        normal "inject no context" outcome to the caller, never an
        exception. Conversation Service calls this exactly once per user
        turn (see services/conversation/pipeline.py)."""
        ...

    async def close(self) -> None: ...


class IKnowledgeAvailabilityRepository(Protocol):
    """The cheap pre-check: does this agent have any enabled KB at all?
    Backed by Redis, write-populated by services/knowledge's
    agent_knowledge_bases write path (same one-writer-per-key principle as
    Config SDK's RedisConfigRepository) — this repository never writes."""

    async def has_enabled_kb(self, tenant_slug: str, agent_slug: str) -> bool | None:
        """None = cache miss (unknown, not "false") — caller decides how to
        treat that (CacheAsideKnowledgeProvider asks the HTTP repository)."""
        ...

    async def close(self) -> None: ...


class IRetrievalRepository(Protocol):
    """Raw dict in, raw dict out — mapping to RetrievedContext is
    CacheAsideKnowledgeProvider's job, matching Config SDK's IConfigRepository
    convention (a repository never imports models.py)."""

    async def retrieve(
        self,
        tenant_slug: str,
        agent_slug: str,
        query: str,
        policy: RetrievalPolicy,
    ) -> dict[str, Any] | None: ...

    async def has_enabled_kb(self, tenant_slug: str, agent_slug: str) -> bool: ...

    async def close(self) -> None: ...
