from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import tenants as tenants_service
from ..auth import CurrentUser
from ..deps import get_current_user, get_or_404, require_role
from ..schemas import TenantCreate, TenantUpdate

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.get("")
async def list_tenants(current_user: CurrentUser = Depends(get_current_user)):
    return await tenants_service.list_tenants()


# Creating/renaming/deleting a tenant is a platform-level action — superadmin
# only, unlike per-tenant resources (agents, providers, ...) where a
# tenant-scoped admin can act within their own tenant.
@router.post("", status_code=201)
async def create_tenant(body: TenantCreate, current_user: CurrentUser = Depends(require_role("superadmin"))):
    return await tenants_service.create_tenant(
        name=body.name, slug=body.slug, region=body.region,
        user_id=current_user.id, user_email=current_user.email,
    )


@router.get("/{slug}")
async def get_tenant(slug: str, current_user: CurrentUser = Depends(get_current_user)):
    return await get_or_404(tenants_service.get_tenant(slug), f"tenant {slug!r} not found")


@router.patch("/{tenant_id}")
async def update_tenant(
    tenant_id: str, body: TenantUpdate, current_user: CurrentUser = Depends(require_role("superadmin")),
):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    return await tenants_service.update_tenant(
        tenant_id, user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.delete("/{tenant_id}", status_code=204)
async def delete_tenant(tenant_id: str, current_user: CurrentUser = Depends(require_role("superadmin"))):
    await tenants_service.soft_delete_tenant(tenant_id, user_id=current_user.id, user_email=current_user.email)
