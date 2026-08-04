"""
Knowledge SDK data transfer objects — the only shapes Conversation Service
(or any future consumer) ever sees. Mirrors libs/config_sdk/models.py's
posture: these are domain objects assembled for the caller, not the raw
Postgres rows services/knowledge/*.py works with internally.

RetrievedContext is deliberately NOT folded into libs.config_sdk.RuntimeConfig
— retrieval is a separate concern (query-dependent, not resolved once per
session) with its own SDK, its own failure mode (no eligible KB is a normal,
cheap "None", not an error), and its own caller contract (exactly one call
per user turn — see services/conversation/pipeline.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetrievalPolicy:
    """Configurable retrieval knobs — never hardcoded constants at a call
    site. Every field defaults to None, meaning "no per-call override" —
    NOT "use some fixed number baked into this SDK". Knowledge Service
    resolves the effective value per request via a three-tier chain (see
    services/knowledge/retrieval.py's _resolve_policy()):

        this call's explicit override  >  the agent's configured
        agent_retrieval_policies row  >  a system-wide fallback default

    So a Support agent (top_k=8), a Sales agent (top_k=3, optimizing for
    speed), and a Legal agent (top_k=15, minimum_score=0.6, stricter
    recall) all get their own behavior from server-side configuration
    (services/knowledge/routers/retrieval_policies.py), without
    Conversation Service or any other caller special-casing agents in
    code. A caller that DOES want to force a specific value for one call
    (e.g. an admin "test this KB" tool) still can, by setting that field.

    Extension points, not implementations, for Phase 6B: rerank and
    hybrid_search are accepted here (and in agent_retrieval_policies) so a
    future reranking/BM25-hybrid step is an additive field read, not a
    signature change — neither is implemented by services/knowledge/
    retrieval.py yet. include_citations controls whether Conversation
    Service should render source attribution into the injected context.
    """
    top_k: int | None = None
    max_tokens: int | None = None
    minimum_score: float | None = None
    rerank: bool | None = None          # Phase 6B extension point — not implemented
    hybrid_search: bool | None = None   # Phase 6B extension point — not implemented
    include_citations: bool | None = None


@dataclass(frozen=True)
class ChunkSource:
    """Provenance for one retrieved chunk — what a caller needs to cite or
    filter by, without re-fetching kb_documents itself."""
    kb_id: str
    kb_slug: str
    document_id: str
    document_title: str
    chunk_id: str
    page: int | None
    language: str | None
    tags: dict[str, Any] = field(default_factory=dict)
    version: int = 1


@dataclass(frozen=True)
class RetrievedChunk:
    content: str
    score: float  # cosine similarity, higher is more relevant
    source: ChunkSource


@dataclass(frozen=True)
class RetrievedContext:
    """Returned by IKnowledgeProvider.retrieve() — never raw chunks/rows.
    Conversation Service reads chunks (to build the LLM context message) and
    the rest for observability; it never talks to pgvector or Postgres
    itself, matching the same boundary Config SDK enforces for config reads.
    """
    chunks: list[RetrievedChunk]
    sources: list[str]          # deduplicated document titles, for citation/logging
    confidence: float           # top chunk's score, 0.0 if chunks is empty
    latency_ms: float           # SDK-side wall-clock for this retrieve() call
    token_count: int            # sum of chunks' approximate token counts
    include_citations: bool = True  # resolved policy's citation setting — see RetrievalPolicy
    retrieval_metadata: dict[str, Any] = field(default_factory=dict)
