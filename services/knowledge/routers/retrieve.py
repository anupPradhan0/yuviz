"""
Internal-only endpoints — the two calls libs.knowledge_sdk's repositories
make. Not meant for the Admin UI; any authenticated identity may call them
(same viewer-role service-account pattern Config SDK's HttpConfigRepository
already uses — see scripts/create_service_account.py), not just superadmin/
admin. A 404 here means "no eligible context", which the SDK's
HttpKnowledgeRepository already maps to None — never a 500 for the normal
"this agent has no KB" case.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from services.config.auth import CurrentUser
from services.config.deps import get_current_user

from .. import agent_kb as agent_kb_service
from .. import db
from .. import retrieval as retrieval_service
from ..runtime import get_embedding_manager, get_vector_repo
from ..schemas import RetrieveRequest

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/agents/{tenant_slug}/{agent_slug}/has-knowledge")
async def has_knowledge(
    tenant_slug: str, agent_slug: str, current_user: CurrentUser = Depends(get_current_user),
):
    enabled = await agent_kb_service.has_enabled_kb(tenant_slug, agent_slug)
    return {"enabled": enabled}


@router.post("/retrieve")
async def retrieve(
    body: RetrieveRequest, current_user: CurrentUser = Depends(get_current_user),
):
    # vector_repo/embedding_manager are lazy module-level singletons (see
    # runtime.py) — constructed once, reused across requests, matching
    # AIProviderManager's own "instantiate once, reuse" contract.
    pool = await db.get_pool()
    result = await retrieval_service.retrieve(
        pool,
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
    )
    if result is None:
        raise HTTPException(status_code=404, detail="no eligible context for this agent/query")
    return result
