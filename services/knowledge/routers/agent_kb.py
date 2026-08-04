from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from services.config.auth import CurrentUser
from services.config.deps import get_current_user, require_role

from .. import agent_kb as agent_kb_service
from ..schemas import AgentKnowledgeBaseCreate, AgentKnowledgeBaseUpdate

router = APIRouter(prefix="/agents/{agent_id}/knowledge-bases", tags=["agent_knowledge_bases"])


@router.get("")
async def list_agent_knowledge_bases(agent_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await agent_kb_service.list_for_agent(agent_id)


@router.post("", status_code=201)
async def assign_knowledge_base(
    agent_id: str,
    body: AgentKnowledgeBaseCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    return await agent_kb_service.assign(agent_id, body.kb_id, enabled=body.enabled)


@router.patch("/{kb_id}")
async def update_assignment(
    agent_id: str,
    kb_id: str,
    body: AgentKnowledgeBaseUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    try:
        return await agent_kb_service.set_enabled(agent_id, kb_id, enabled=body.enabled)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/{kb_id}", status_code=204)
async def detach_knowledge_base(
    agent_id: str, kb_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await agent_kb_service.detach(agent_id, kb_id)
