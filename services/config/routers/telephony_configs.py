from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import telephony_configs as telephony_configs_service
from .. import tenants as tenants_service
from ..auth import CurrentUser
from ..deps import get_current_user, get_or_404, require_role, validate_id_exists
from ..schemas import TelephonyConfigCreate, TelephonyConfigUpdate

tenant_scoped_router = APIRouter(prefix="/tenants/{tenant_id}/telephony-configs", tags=["telephony_configs"])
router = APIRouter(prefix="/telephony-configs", tags=["telephony_configs"])
providers_router = APIRouter(tags=["telephony_configs"])


async def _resolve_tenant_id(tenant_id: str) -> None:
    await validate_id_exists(tenant_id, tenants_service.get_tenant_by_id, "tenant")


@tenant_scoped_router.get("")
async def list_telephony_configs(
    tenant_id: str, current_user: CurrentUser = Depends(get_current_user),
):
    return await telephony_configs_service.list_telephony_configs(tenant_id)


@tenant_scoped_router.post("", status_code=201)
async def create_telephony_config(
    tenant_id: str,
    body: TelephonyConfigCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _resolve_tenant_id(tenant_id)
    return await telephony_configs_service.create_telephony_config(
        tenant_id=tenant_id,
        name=body.name,
        provider=body.provider,
        credentials=body.credentials,
        is_default_outbound=body.is_default_outbound,
        user_id=current_user.id,
        user_email=current_user.email,
    )


@router.get("/{config_id}")
async def get_telephony_config(config_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await get_or_404(
        telephony_configs_service.get_telephony_config(config_id),
        f"telephony_config {config_id!r} not found",
    )


@router.patch("/{config_id}")
async def update_telephony_config(
    config_id: str,
    body: TelephonyConfigUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    return await telephony_configs_service.update_telephony_config(
        config_id, user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.post("/{config_id}/set-default-outbound")
async def set_default_outbound(
    config_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    return await telephony_configs_service.set_default_outbound(
        config_id, user_id=current_user.id, user_email=current_user.email,
    )


@router.delete("/{config_id}", status_code=204)
async def delete_telephony_config(
    config_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await telephony_configs_service.soft_delete_telephony_config(
        config_id, user_id=current_user.id, user_email=current_user.email,
    )


@providers_router.get("/telephony-providers")
async def list_supported_providers(current_user: CurrentUser = Depends(get_current_user)):
    """Discovery endpoint (Dograh's "List Supported Providers" equivalent) —
    name -> required credential fields, so an admin UI can render the right
    form per provider without hardcoding field lists."""
    return telephony_configs_service.list_supported_providers()
