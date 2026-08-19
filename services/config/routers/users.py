from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import users as users_service
from ..auth import CurrentUser
from ..deps import get_current_user, require_role
from ..schemas import UserCreate, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("")
async def list_users(current_user: CurrentUser = Depends(get_current_user)):
    # Superadmin sees every user; a tenant-scoped admin/viewer sees only
    # their own tenant's users — same scoping principle as every other
    # tenant-owned resource in this API.
    users = await users_service.list_users(
        tenant_id=current_user.tenant_id, is_superadmin=(current_user.role == "superadmin"),
    )
    return [users_service.to_public_dict(u) for u in users]


@router.post("", status_code=201)
async def create_user(
    body: UserCreate, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    user = await users_service.create_user(
        email=body.email,
        password=body.password,
        role=body.role,
        tenant_id=body.tenant_id,
        creator_user_id=current_user.id,
        creator_user_email=current_user.email,
    )
    return users_service.to_public_dict(user)


@router.patch("/{user_id}")
async def update_user(
    user_id: str,
    body: UserUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin")),
):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    user = await users_service.update_user(
        user_id, actor_user_id=current_user.id, actor_user_email=current_user.email, **fields,
    )
    return users_service.to_public_dict(user)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: str, current_user: CurrentUser = Depends(require_role("superadmin")),
):
    await users_service.soft_delete_user(
        user_id, actor_user_id=current_user.id, actor_user_email=current_user.email,
    )
