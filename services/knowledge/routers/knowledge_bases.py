from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from services.config.auth import CurrentUser
from services.config.deps import get_current_user, require_role

from .. import knowledge_bases as kb_service
from ..schemas import KnowledgeBaseCreate, KnowledgeBaseUpdate

tenant_scoped_router = APIRouter(prefix="/tenants/{tenant_id}/knowledge-bases", tags=["knowledge_bases"])
router = APIRouter(prefix="/knowledge-bases", tags=["knowledge_bases"])


@tenant_scoped_router.get("")
async def list_knowledge_bases(tenant_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await kb_service.list_knowledge_bases(tenant_id)


@tenant_scoped_router.post("", status_code=201)
async def create_knowledge_base(
    tenant_id: str,
    body: KnowledgeBaseCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    return await kb_service.create_knowledge_base(
        tenant_id=tenant_id,
        slug=body.slug,
        name=body.name,
        description=body.description,
        embedding_config_id=body.embedding_config_id,
        user_id=current_user.id,
        user_email=current_user.email,
    )


@router.get("/{kb_id}")
async def get_knowledge_base(kb_id: str, current_user: CurrentUser = Depends(get_current_user)):
    kb = await kb_service.get_knowledge_base(kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail=f"knowledge_base {kb_id!r} not found")
    return kb


@router.patch("/{kb_id}")
async def update_knowledge_base(
    kb_id: str,
    body: KnowledgeBaseUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    return await kb_service.update_knowledge_base(
        kb_id, user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.delete("/{kb_id}", status_code=204)
async def delete_knowledge_base(
    kb_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await kb_service.soft_delete_knowledge_base(kb_id, user_id=current_user.id, user_email=current_user.email)
