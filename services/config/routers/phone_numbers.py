from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException

from libs.tenancy import set_target_tenant

from .. import agents as agents_service
from .. import carriers as carriers_service
from .. import phone_numbers as phone_numbers_service
from .. import telephony_configs as telephony_configs_service
from .. import tenants as tenants_service
from ..auth import CurrentUser
from ..deps import (
    assert_tenant_access,
    bind_path_tenant,
    get_current_user,
    get_or_404,
    is_platform_scoped,
    require_path_tenant_access,
    require_role,
    validate_id_exists,
)
from ..schemas import PhoneNumberCreate, PhoneNumberUpdate

tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/phone-numbers",
    tags=["phone_numbers"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)
router = APIRouter(prefix="/phone-numbers", tags=["phone_numbers"])


async def _resolve_tenant_id(tenant_id: str) -> None:
    """Same shape as provider_configs router's _resolve_tenant_id — a clean
    400/404 instead of an INSERT's FK violation reaching the client as a
    raw 500."""
    await validate_id_exists(tenant_id, tenants_service.get_tenant_by_id, "tenant")


async def _resolve_agent_id(agent_id: str | None) -> None:
    await validate_id_exists(agent_id, agents_service.get_agent_by_id, "agent")


async def _resolve_carrier_id(carrier_id: str | None) -> None:
    await validate_id_exists(carrier_id, carriers_service.get_carrier_by_id, "carrier")


async def _resolve_telephony_config_id(telephony_config_id: str | None, tenant_id: str) -> None:
    if telephony_config_id is None:
        return
    cfg = await telephony_configs_service.get_telephony_config(telephony_config_id)
    # str(): a fresh row carries a UUID, a cache hit a str (see agents.py).
    if cfg is None or str(cfg.get("tenant_id")) != str(tenant_id):
        raise HTTPException(status_code=404, detail="telephony_config not found")


_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def _is_local_address(did: str) -> bool:
    """Inbound routing keys on the DID alone, so anything that isn't a
    public number or SIP URI is an extension on the shared Kamailio/FreeSWITCH."""
    return not (_E164.match(did) or did.lower().startswith("sip:"))


async def _require_superadmin_for_local_number(
    current_user: CurrentUser, did: str, telephony_config_id: str | None, carrier_id: str | None,
) -> None:
    """A local extension, a number under a Native config, or one under no
    provider is a local SIP number on the platform's shared Kamailio/
    FreeSWITCH. Only the platform assigns those: a tenant choosing its own
    local extension could claim one that routes to another tenant's phones."""
    if current_user.role == "superadmin":
        return
    local = _is_local_address(did) or (telephony_config_id is None and carrier_id is None)
    if not local and telephony_config_id is not None:
        cfg = await telephony_configs_service.get_telephony_config(telephony_config_id, platform_scoped=True)
        # A config that no longer resolves fails closed.
        local = cfg is None or cfg["provider"] == telephony_configs_service.NATIVE_PROVIDER
    if local:
        raise HTTPException(
            status_code=403,
            detail="Local numbers and extensions are assigned by the platform; enter public numbers in E.164 form (+...)",
        )


@tenant_scoped_router.get("")
async def list_phone_numbers(tenant_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await phone_numbers_service.list_phone_numbers(tenant_id)


@tenant_scoped_router.post("", status_code=201)
async def create_phone_number(
    tenant_id: str,
    body: PhoneNumberCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _resolve_tenant_id(tenant_id)
    await _resolve_agent_id(body.agent_id)
    await _resolve_agent_id(body.fallback_agent_id)
    await _resolve_carrier_id(body.carrier_id)
    await _resolve_telephony_config_id(body.telephony_config_id, tenant_id)
    await _require_superadmin_for_local_number(current_user, body.did, body.telephony_config_id, body.carrier_id)
    return await phone_numbers_service.create_phone_number(
        tenant_id=tenant_id,
        did=body.did,
        agent_id=body.agent_id,
        fallback_agent_id=body.fallback_agent_id,
        carrier_id=body.carrier_id,
        telephony_config_id=body.telephony_config_id,
        region=body.region,
        status=body.status,
        user_id=current_user.id,
        user_email=current_user.email,
    )


@router.get("/{phone_number_id}")
async def get_phone_number(phone_number_id: str, current_user: CurrentUser = Depends(get_current_user)):
    phone_number = await get_or_404(
        phone_numbers_service.get_phone_number(
            phone_number_id, platform_scoped=is_platform_scoped(current_user),
        ),
        f"phone_number {phone_number_id!r} not found",
    )
    await assert_tenant_access(phone_number["tenant_id"], current_user)
    return phone_number


@router.patch("/{phone_number_id}")
async def update_phone_number(
    phone_number_id: str,
    body: PhoneNumberUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    phone_number = await get_or_404(
        phone_numbers_service.get_phone_number(
            phone_number_id, platform_scoped=is_platform_scoped(current_user),
        ),
        f"phone_number {phone_number_id!r} not found",
    )
    await assert_tenant_access(phone_number["tenant_id"], current_user)
    # Resolvers below use tenant_conn() (RLS-scoped), so the tenant must be
    # bound before they run — otherwise the very first one raises
    # TenantUnresolved regardless of which tenant owns the referenced row.
    set_target_tenant(phone_number["tenant_id"])
    if "agent_id" in fields:
        await _resolve_agent_id(fields["agent_id"])
    if "fallback_agent_id" in fields:
        await _resolve_agent_id(fields["fallback_agent_id"])
    if "carrier_id" in fields:
        await _resolve_carrier_id(fields["carrier_id"])
    if "telephony_config_id" in fields:
        await _resolve_telephony_config_id(fields["telephony_config_id"], phone_number["tenant_id"])
    if {"did", "telephony_config_id", "carrier_id"} & fields.keys():
        # Both where the number is now and where it would end up.
        await _require_superadmin_for_local_number(
            current_user, phone_number["did"], phone_number.get("telephony_config_id"), phone_number.get("carrier_id"),
        )
        await _require_superadmin_for_local_number(
            current_user,
            fields.get("did") or phone_number["did"],
            fields.get("telephony_config_id", phone_number.get("telephony_config_id")),
            fields.get("carrier_id", phone_number.get("carrier_id")),
        )
    return await phone_numbers_service.update_phone_number(
        phone_number_id, user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.delete("/{phone_number_id}", status_code=204)
async def delete_phone_number(
    phone_number_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    phone_number = await get_or_404(
        phone_numbers_service.get_phone_number(
            phone_number_id, platform_scoped=is_platform_scoped(current_user),
        ),
        f"phone_number {phone_number_id!r} not found",
    )
    await assert_tenant_access(phone_number["tenant_id"], current_user)
    # Releasing a local number is as platform-owned as assigning one: a tenant
    # admin who deleted it could never add it back. They can set it inactive.
    await _require_superadmin_for_local_number(
        current_user, phone_number["did"], phone_number.get("telephony_config_id"), phone_number.get("carrier_id"),
    )
    set_target_tenant(phone_number["tenant_id"])
    await phone_numbers_service.soft_delete_phone_number(
        phone_number_id, user_id=current_user.id, user_email=current_user.email,
    )
