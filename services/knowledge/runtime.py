"""Lazy singletons for routers; not on app.state because ASGITransport tests skip lifespan."""

from __future__ import annotations

import logging

from .embedding_manager import EmbeddingProviderManager
from .retrieval import IReranker, NoopReranker
from .reranker import RerankError, build_reranker
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


class _UnavailableReranker:
    """Stands in when the configured reranker could not be built: every call reports rerank_not_configured (and
    retrieval answers with the fused order) instead of failing retrieval."""

    configured = True

    async def rerank(self, query, matches, tenant_id=None):
        raise RerankError("rerank_not_configured", breaker_failure=False)


def get_reranker() -> IReranker:
    """KNOWLEDGE_RERANKER=cohere|voyage picks a hosted reranker; the default is none, and rerank=true is then
    reported as rerank_skipped. Built ONCE per process; a construction error never reaches a retrieval."""
    global _reranker
    if _reranker is None:
        try:
            _reranker = build_reranker() or NoopReranker()
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).error("reranker could not be built (%s): rerank is unavailable", type(exc).__name__)
            _reranker = _UnavailableReranker()
    return _reranker


def init_reranker() -> IReranker:
    """Service startup: an unusable KNOWLEDGE_RERANK_URL (not https, host not allowed) REFUSES to start, because the
    alternative is to send the platform key somewhere unintended. Anything else falls back as get_reranker()."""
    global _reranker
    try:
        _reranker = build_reranker() or NoopReranker()
    except ValueError as exc:
        raise RuntimeError(f"invalid reranker configuration: {exc}") from None
    except Exception:  # noqa: BLE001
        _reranker = None
        return get_reranker()
    return _reranker


def set_reranker(reranker: IReranker | None) -> None:
    """Tests: install a reranker, or None to rebuild from the environment at the next call."""
    global _reranker
    _reranker = reranker
