"""
Pydantic request models for Knowledge Service's HTTP API — same convention
as services/config/schemas.py: responses are the plain dicts the service
modules already return, no separate response schema.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class KnowledgeBaseCreate(BaseModel):
    slug: str
    name: str
    description: str = ""
    embedding_config_id: str | None = None


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    embedding_config_id: str | None = None
    status: str | None = None


class DocumentUpdate(BaseModel):
    title: str | None = None
    language: str | None = None
    tags: dict[str, Any] | None = None
    # 'auto': retrieved only when relevant. 'prompt': always injected into
    # the LLM context every turn — see kb_documents.usage_mode in
    # database/knowledge_schema.sql. An admin flips this manually for a
    # small/critical reference doc; the ingestion worker also sets it
    # automatically for documents under AUTO_INLINE_THRESHOLD_BYTES.
    usage_mode: str | None = None


class AgentKnowledgeBaseCreate(BaseModel):
    kb_id: str
    enabled: bool = True


class AgentKnowledgeBaseUpdate(BaseModel):
    enabled: bool


class RetrieveRequest(BaseModel):
    tenant_slug: str
    agent_slug: str
    query: str
    # None = no per-call override — retrieval.py's _resolve_policy() falls
    # back to the agent's agent_retrieval_policies row, then a system
    # default. Never defaulted to a fixed number here — see
    # libs/knowledge_sdk/models.py's RetrievalPolicy docstring.
    top_k: int | None = None
    max_tokens: int | None = None
    minimum_score: float | None = None
    rerank: bool | None = None
    hybrid_search: bool | None = None
    include_citations: bool | None = None


class RetrievalPolicyUpdate(BaseModel):
    top_k: int | None = None
    max_tokens: int | None = None
    minimum_score: float | None = None
    rerank: bool | None = None
    hybrid_search: bool | None = None
    include_citations: bool | None = None
