from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import agents as agents_service
from .. import tenants as tenants_service
from ..auth import CurrentUser
from ..deps import get_current_user, get_or_404, require_role
from ..schemas import AgentCreate, AgentUpdate

router = APIRouter(prefix="/tenants/{tenant_slug}/agents", tags=["agents"])


async def _resolve_tenant(tenant_slug: str) -> dict:
    return await get_or_404(
        tenants_service.get_tenant(tenant_slug), f"tenant {tenant_slug!r} not found",
    )


@router.get("")
async def list_agents(tenant_slug: str, current_user: CurrentUser = Depends(get_current_user)):
    tenant = await _resolve_tenant(tenant_slug)
    return await agents_service.list_agents(tenant["id"])


@router.post("", status_code=201)
async def create_agent(
    tenant_slug: str,
    body: AgentCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    tenant = await _resolve_tenant(tenant_slug)
    return await agents_service.create_agent(
        tenant_id=tenant["id"],
        slug=body.slug,
        name=body.name,
        greeting=body.greeting,
        system_prompt=body.system_prompt,
        stt_config_id=body.stt_config_id,
        llm_config_id=body.llm_config_id,
        tts_config_id=body.tts_config_id,
        tenant_slug=tenant_slug,
        user_id=current_user.id,
        user_email=current_user.email,
    )


@router.get("/{agent_slug}")
async def get_agent(tenant_slug: str, agent_slug: str, current_user: CurrentUser = Depends(get_current_user)):
    return await get_or_404(
        agents_service.get_agent(tenant_slug, agent_slug),
        f"agent {agent_slug!r} not found under tenant {tenant_slug!r}",
    )


@router.patch("/{agent_id}")
async def update_agent(
    tenant_slug: str,
    agent_id: str,
    body: AgentUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    return await agents_service.update_agent(
        agent_id, tenant_slug=tenant_slug,
        user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    tenant_slug: str,
    agent_id: str,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await agents_service.soft_delete_agent(
        agent_id, tenant_slug=tenant_slug, user_id=current_user.id, user_email=current_user.email,
    )
