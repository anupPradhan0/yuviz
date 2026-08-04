"""
agent_resolver — resolves an (tenant_slug, agent_slug) pair into a
(RuntimeConfig, ProviderBundle) pair, or signals "not available" so the
caller falls back to the legacy YAML/env-var path (see agent_config.py's
to_runtime_config() for how that path produces the same shape).

Depends only on libs.config_sdk's IConfigProvider and
services.conversation.provider_bundle's ProviderRegistry — never imports
services.config directly (that boundary is the whole point of the SDK; see
libs/config_sdk/__init__.py's docstring). Conversation Service has no idea
whether a given call's config came from Redis or Config Service's REST API,
and doesn't need to.

resolve_handler_deps() never raises: any failure along the way (config
service unreachable, no matching agent row, a provider role that doesn't
resolve to a usable provider_configs row) returns None. This mirrors the
gateway's RedisClient/TenantConfig::from_redis and services/config/cache.py
— a config-plane problem must degrade to defaults, never reject or crash a
call.

Exactly one IConfigProvider call (get_runtime_config()) plus one
ProviderRegistry.resolve() call — no repeated Redis/HTTP lookups after
that; RuntimeConfig is immutable for the lifetime of the session.
"""

from __future__ import annotations

import logging

from libs.config_sdk import IConfigProvider, RuntimeConfig

from .provider_bundle import ProviderBundle, ProviderRegistry

log = logging.getLogger(__name__)


async def resolve_handler_deps(
    tenant_slug: str,
    agent_slug: str,
    registry: ProviderRegistry,
    config: IConfigProvider,
) -> tuple[RuntimeConfig, ProviderBundle] | None:
    try:
        runtime_config = await config.get_runtime_config(tenant_slug, agent_slug)
        if runtime_config is None:
            # Covers every "not available" case in one signal: no agent row,
            # agent.status != 'active' (deactivated, not deleted), no tenant
            # row, or an incomplete provider assignment (all-or-nothing —
            # see IConfigProvider.get_runtime_config()'s own contract). The
            # SDK doesn't distinguish these to its caller; agent_resolver.py
            # doesn't need to, either.
            log.info(
                "agent_resolver: no runtime config for tenant=%s agent=%s "
                "— falling back to legacy config",
                tenant_slug, agent_slug,
            )
            return None

        bundle = await registry.resolve(runtime_config.providers)
        return runtime_config, bundle
    except Exception:
        log.exception(
            "agent_resolver: resolution failed tenant=%s agent=%s — falling back to legacy config",
            tenant_slug, agent_slug,
        )
        return None
