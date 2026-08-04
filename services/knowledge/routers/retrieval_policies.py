from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from services.config.auth import CurrentUser
from services.config.deps import get_current_user, require_role

from .. import retrieval_policies as policy_service
from ..schemas import RetrievalPolicyUpdate

router = APIRouter(prefix="/agents/{agent_id}/retrieval-policy", tags=["retrieval_policies"])


@router.get("")
async def get_retrieval_policy(agent_id: str, current_user: CurrentUser = Depends(get_current_user)):
    policy = await policy_service.get_policy(agent_id)
    return policy or {"agent_id": agent_id}


@router.put("")
async def set_retrieval_policy(
    agent_id: str,
    body: RetrievalPolicyUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to set")
    return await policy_service.upsert_policy(
        agent_id, user_id=current_user.id, user_email=current_user.email, **fields,
    )
