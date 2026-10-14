"""Internal endpoints for libs.knowledge_sdk (any authenticated identity). /retrieve always answers 200 with a
`status` (ok | no_match | partial | unavailable): "nothing relevant" and "retrieval is broken" must never look alike.
The tenant comes from the body/path, so each handler must assert_tenant_access before set_target_tenant."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends

from libs.tenancy import set_target_tenant
from services.config.auth import CurrentUser
from services.config.deps import assert_tenant_access, get_current_user

from .. import agent_kb as agent_kb_service
from .. import retrieval as retrieval_service
from ..resilience import RetrievalUnavailable
from ..runtime import get_embedding_manager, get_reranker, get_vector_repo
from ..schemas import RetrieveRequest

log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

_STATUSES = frozenset({"ok", "no_match", "partial", "unavailable"})
# Outer guard only; stage deadlines live in retrieval.py. Under the caller's per-tool timeout, so the
# service answers "unavailable" itself rather than being cut off mid-request.
_RETRIEVE_TIMEOUT_ENV = "KNOWLEDGE_RETRIEVE_TIMEOUT_S"
_DEFAULT_RETRIEVE_TIMEOUT_S = 1.2


def _timeout_s() -> float:
    return float(os.environ.get(_RETRIEVE_TIMEOUT_ENV, _DEFAULT_RETRIEVE_TIMEOUT_S))


def _unavailable(trace_id: str, reasons: list[str]) -> dict[str, Any]:
    """Safe by construction: fixed reason codes, never the exception text (it can carry URLs and credentials)."""
    return {
        "status": "unavailable", "chunks": [], "sources": [], "token_count": 0,
        "degraded": reasons, "trace_id": trace_id,
    }


def _envelope(result: dict[str, Any] | None, trace_id: str) -> dict[str, Any]:
    if result is None:
        return {"status": "no_match", "chunks": [], "sources": [], "token_count": 0, "trace_id": trace_id}
    body = dict(result)
    if body.get("status") not in _STATUSES:  # a retrieve() that predates the status contract
        body["status"] = "ok" if body.get("chunks") else "no_match"
    body.setdefault("trace_id", trace_id)
    return body


@router.get("/agents/{tenant_slug}/{agent_slug}/has-knowledge")
async def has_knowledge(
    tenant_slug: str, agent_slug: str, current_user: CurrentUser = Depends(get_current_user),
):
    await assert_tenant_access(tenant_slug, current_user)
    set_target_tenant(tenant_slug)
    enabled = await agent_kb_service.has_enabled_kb(tenant_slug, agent_slug)
    return {"enabled": enabled}


@router.post("/retrieve")
async def retrieve(
    body: RetrieveRequest, current_user: CurrentUser = Depends(get_current_user),
):
    await assert_tenant_access(body.tenant_slug, current_user)
    set_target_tenant(body.tenant_slug)
    trace_id = uuid.uuid4().hex[:16]
    try:
        async with asyncio.timeout(_timeout_s()):
            # No connection is held here: retrieve() takes its own short-lived ones through its admission
            # limiter, so a request it refuses costs the pool nothing.
            result = await retrieval_service.retrieve(
                None,
                await get_vector_repo(),
                get_embedding_manager(),
                tenant_slug=body.tenant_slug,
                agent_slug=body.agent_slug,
                query=body.query,
                top_k=body.top_k,
                max_tokens=body.max_tokens,
                minimum_score=body.minimum_score,
                rerank=body.rerank,
                hybrid_search=body.hybrid_search,
                include_citations=body.include_citations,
                trace_id=trace_id,
                reranker=get_reranker(),
            )
    except TimeoutError:
        log.warning("retrieve timed out tenant=%s agent=%s trace=%s", body.tenant_slug, body.agent_slug, trace_id)
        return _unavailable(trace_id, ["timeout"])
    except RetrievalUnavailable as exc:
        # Overload, or every branch failed: an expected, typed outcome, so a WARNING and no traceback.
        log.warning(
            "retrieve unavailable tenant=%s agent=%s trace=%s degraded=%s",
            body.tenant_slug, body.agent_slug, trace_id, exc.degraded,
        )
        return _unavailable(exc.trace_id or trace_id, list(exc.degraded) or ["retrieval_error"])
    except Exception:
        log.exception("retrieve failed tenant=%s agent=%s trace=%s", body.tenant_slug, body.agent_slug, trace_id)
        return _unavailable(trace_id, ["retrieval_error"])
    return _envelope(result, trace_id)
