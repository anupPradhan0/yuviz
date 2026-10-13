"""Lazy singletons for routers; not on app.state because ASGITransport tests skip lifespan."""

from __future__ import annotations

from .embedding_manager import EmbeddingProviderManager
from .retrieval import IReranker, NoopReranker
from .reranker import build_reranker
from .secret_resolver import CompositeSecretResolver
from .vector_repository import PgVectorRepository

_vector_repo: PgVectorRepository | None = None
_embedding_manager: EmbeddingProviderManager | None = None


async def get_vector_repo() -> PgVectorRepository:
    global _vector_repo
    if _vector_repo is None:
        _vector_repo = PgVectorRepository()
    return _vector_repo


def get_embedding_manager() -> EmbeddingProviderManager:
    global _embedding_manager
    if _embedding_manager is None:
        _embedding_manager = EmbeddingProviderManager(CompositeSecretResolver())
    return _embedding_manager


_reranker: IReranker | None = None


def get_reranker() -> IReranker:
    """KNOWLEDGE_RERANKER=cohere|voyage picks a hosted reranker; the default is none, and rerank=true is then
    reported as rerank_skipped. Built once per process (env is read at first use)."""
    global _reranker
    if _reranker is None:
        _reranker = build_reranker() or NoopReranker()
    return _reranker


def set_reranker(reranker: IReranker | None) -> None:
    """Tests: install a reranker, or None to rebuild from the environment at the next call."""
    global _reranker
    _reranker = reranker
