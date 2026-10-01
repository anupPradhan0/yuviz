"""
Keeps a REST provider's inbound routing in step with phone_numbers, so an
admin never has to wire a number up by hand in the provider's dashboard:
adding a number under a provider config points that number's calls at the
telephony service, removing it stops them. Cold path only (Config's CRUD
routes), never on a live call.

A sync failure never loses the admin's change: create/update return the
outcome as `provider_sync` alongside the saved number. Delete is the
exception, see routers/phone_numbers.py.
"""

from __future__ import annotations

import json as _json
import logging
import os
from typing import Any

from libs.config_sdk.secrets import decrypt_secret, is_encrypted
from libs.telephony_sdk.exceptions import TelephonyProviderError
from libs.telephony_sdk.interface import InboundUrls, ITelephonyProvider
from libs.telephony_sdk.registry import TelephonyProviderRegistry
from libs.tenancy import platform_conn

from . import audit, cache, db, telephony_configs

log = logging.getLogger(__name__)

PUBLIC_BASE_URL_ENV = "TELEPHONY_PUBLIC_BASE_URL"


class NumberNotInAccount(Exception):
    def __init__(self, did: str, provider: str) -> None:
        super().__init__(f"{did} isn't a number in this {provider} account")


class ProviderUnreachable(Exception):
    def __init__(self, provider: str, detail: str) -> None:
        super().__init__(f"couldn't check the number with {provider}: {detail}")


def _decrypt(value: Any) -> Any:
    if isinstance(value, list):
        return [_decrypt(v) for v in value]
    if isinstance(value, str) and is_encrypted(value):
        return decrypt_secret(value)
    return value


def _provider_for(config: dict[str, Any]) -> ITelephonyProvider | None:
    name = config["provider"]
    if name == telephony_configs.NATIVE_PROVIDER or name not in TelephonyProviderRegistry.all():
        return None
    provider_cls = TelephonyProviderRegistry.get(name)
    credentials = dict(config.get("credentials") or {})
    for field in provider_cls.sensitive_credential_fields():
        if field in credentials:
            credentials[field] = _decrypt(credentials[field])
    return provider_cls(credentials)


def inbound_urls(config: dict[str, Any]) -> InboundUrls | None:
    base = os.environ.get(PUBLIC_BASE_URL_ENV, "").rstrip("/")
    if not base:
        return None
    path = f"{config['provider']}/{{}}/{config['id']}"
    return InboundUrls(answer_url=f"{base}/{path.format('voice')}", hangup_url=f"{base}/{path.format('status')}")


async def ensure_owned(config: dict[str, Any], did: str) -> None:
    """Refuses a number the provider says isn't in the account. A provider
    that can't check (None) is allowed through."""
    provider = _provider_for(config)
    if provider is None:
        return
    try:
        owned = await provider.owns_number(did)
    except TelephonyProviderError as exc:
        raise ProviderUnreachable(config["provider"], str(exc)) from None
    if owned is False:
        raise NumberNotInAccount(did, config["provider"])


async def attach(config: dict[str, Any], did: str) -> dict[str, Any] | None:
    """None when the config has nothing to sync (native, carriers)."""
    provider = _provider_for(config)
    if provider is None:
        return None
    urls = inbound_urls(config)
    if urls is None:
        return {"ok": False, "message": f"{PUBLIC_BASE_URL_ENV} isn't set on the Config service, so the "
                                         "provider can't be pointed at this platform"}
    result = await provider.attach_inbound(did, urls, label=f"Yuviz - {config['name']}")
    if result.credentials_update:
        await _merge_credentials(config, result.credentials_update)
    if not result.ok:
        log.warning("number_sync: attach failed config=%s did=%s: %s", config["id"], did, result.message)
    return {"ok": result.ok, "message": result.message}


async def detach(config: dict[str, Any], did: str) -> dict[str, Any] | None:
    provider = _provider_for(config)
    if provider is None:
        return None
    result = await provider.detach_inbound(did)
    return {"ok": result.ok, "message": result.message}


async def _merge_credentials(config: dict[str, Any], update: dict[str, Any]) -> None:
    pool = await db.get_pool()
    async with platform_conn(pool, reason="number-sync-provider-ids", stamp_tenant=str(config["tenant_id"])) as conn:
        await conn.execute(
            "UPDATE telephony_configs SET credentials = credentials || $2::jsonb, updated_at = now() "
            "WHERE id = $1 AND tenant_id = $3",
            config["id"], _json.dumps(update), config["tenant_id"],
        )
        await audit.write_audit(
            conn, entity_type="telephony_config", entity_id=config["id"], action="updated",
            user_id=None, user_email="number-sync", new_value=update,
        )
    await cache.invalidate(telephony_configs._cache_key(config["id"]))
