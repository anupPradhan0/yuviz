from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import provider_configs as provider_configs_service
from .. import tenants as tenants_service
from ..auth import CurrentUser
from ..deps import get_current_user, get_or_404, require_role, validate_id_exists
from ..schemas import ProviderConfigCreate, ProviderConfigUpdate
from ..secret_resolver import CompositeSecretResolver

tenant_scoped_router = APIRouter(prefix="/tenants/{tenant_id}/providers", tags=["provider_configs"])
router = APIRouter(prefix="/providers", tags=["provider_configs"])
_secret_resolver = CompositeSecretResolver()


async def _resolve_tenant_id(tenant_id: str) -> None:
    """Raises a clean 400/404 for a malformed or nonexistent tenant_id,
    instead of letting the INSERT's FK constraint violation reach the
    client as an unhandled 500 with a Postgres constraint name in it."""
    await validate_id_exists(tenant_id, tenants_service.get_tenant_by_id, "tenant")


@tenant_scoped_router.get("")
async def list_provider_configs(
    tenant_id: str,
    role: Literal["stt", "llm", "tts", "embedding"] | None = Query(default=None),
    environment: Literal["prod", "staging", "dev"] | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await provider_configs_service.list_provider_configs(
        tenant_id, role=role, environment=environment,
    )


@tenant_scoped_router.post("", status_code=201)
async def create_provider_config(
    tenant_id: str,
    body: ProviderConfigCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _resolve_tenant_id(tenant_id)
    return await provider_configs_service.create_provider_config(
        tenant_id=tenant_id,
        name=body.name,
        role=body.role,
        engine=body.engine,
        environment=body.environment,
        model=body.model,
        voice=body.voice,
        language=body.language,
        region=body.region,
        api_key_ref=body.api_key_ref,
        extra=body.extra,
        user_id=current_user.id,
        user_email=current_user.email,
    )


async def _authorize_provider(provider_id: str, current_user: CurrentUser) -> dict:
    """404 if the provider doesn't exist; 403 if it exists but belongs to a
    different tenant than the caller. Superadmin (tenant_id is None — see
    auth.py's CurrentUser docstring) is exempt, the same "unscoped" contract
    used everywhere else in this service. Without this check, any
    authenticated admin/viewer could read, edit, or delete another tenant's
    provider_config by id, and list_provider_voices would resolve *that*
    tenant's real api_key_ref and burn its ElevenLabs quota using their key."""
    cfg = await get_or_404(
        provider_configs_service.get_provider_config(provider_id),
        f"provider_config {provider_id!r} not found",
    )
    if current_user.tenant_id is not None and str(cfg["tenant_id"]) != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="provider_config belongs to a different tenant")
    return cfg


@router.get("/{provider_id}")
async def get_provider_config(provider_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await _authorize_provider(provider_id, current_user)


@router.patch("/{provider_id}")
async def update_provider_config(
    provider_id: str,
    body: ProviderConfigUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_provider(provider_id, current_user)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    return await provider_configs_service.update_provider_config(
        provider_id, user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.delete("/{provider_id}", status_code=204)
async def delete_provider_config(
    provider_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_provider(provider_id, current_user)
    await provider_configs_service.soft_delete_provider_config(
        provider_id, user_id=current_user.id, user_email=current_user.email,
    )


@router.get("/{provider_id}/voices")
async def list_provider_voices(provider_id: str, current_user: CurrentUser = Depends(get_current_user)):
    await _authorize_provider(provider_id, current_user)
    return await provider_configs_service.list_elevenlabs_voices(
        provider_id, secret_resolver=_secret_resolver,
    )
