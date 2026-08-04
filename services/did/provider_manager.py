"""
DidProviderManager — creates and caches IDidProvider instances per distinct
carriers row, keyed by carrier_id. Mirrors ToolProviderManager/
AIProviderManager exactly (see project memory
did-management-platform-architecture principle #11): a registry of
factories keyed by carriers.provider, not a growing if/elif chain — adding
Bandwidth or a regional SIP carrier means writing one new class and
registering it here, never touching this manager's own logic.

_DEFAULT_REGISTRY has two entries — Plivo (2026-07-23) and Twilio
(2026-07-26) — both added at the user's explicit request, ahead of having
real trial-account credentials to confirm live API behavior against.
providers/plivo.py and providers/twilio.py are both built from
documentation only and say so loudly in their own module docstrings; do
not treat their presence here as proof either one works. Once real
credentials exist, live-verify each the same way Cal.com/Gemini were, and
only then remove that provider's warning.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

from .secret_resolver import SecretResolver

log = logging.getLogger(__name__)

# A carriers table row (id, tenant_id, name, provider, auth_id,
# auth_token_ref, carrier_account_ref, ...) — this manager only reads it,
# never writes it (carriers.py in services/config owns that).
CarrierRecord = dict[str, Any]
ProviderFactory = Callable[[CarrierRecord, str | None], Awaitable[Any]]


async def _make_plivo(carrier: CarrierRecord, auth_token: str | None) -> Any:
    from .providers.plivo import PlivoProvider

    if not carrier.get("auth_id"):
        raise ValueError(f"carrier id={carrier['id']!r} engine='plivo' has no auth_id configured")
    if not auth_token:
        raise ValueError(f"carrier id={carrier['id']!r} engine='plivo' has no auth_token_ref configured")
    return PlivoProvider(auth_id=carrier["auth_id"], auth_token=auth_token)


async def _make_twilio(carrier: CarrierRecord, auth_token: str | None) -> Any:
    from .providers.twilio import TwilioProvider

    if not carrier.get("auth_id"):
        raise ValueError(f"carrier id={carrier['id']!r} engine='twilio' has no auth_id configured")
    if not auth_token:
        raise ValueError(f"carrier id={carrier['id']!r} engine='twilio' has no auth_token_ref configured")
    return TwilioProvider(account_sid=carrier["auth_id"], auth_token=auth_token)


_DEFAULT_REGISTRY: dict[str, ProviderFactory] = {
    "plivo": _make_plivo,
    "twilio": _make_twilio,
}


class DidProviderManager:
    def __init__(
        self, secret_resolver: SecretResolver, registry: dict[str, ProviderFactory] | None = None,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._registry = dict(_DEFAULT_REGISTRY) if registry is None else dict(registry)
        self._instances: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get(self, carrier: CarrierRecord) -> Any:
        carrier_id = str(carrier["id"])
        cached = self._instances.get(carrier_id)
        if cached is not None:
            return cached

        async with self._locks[carrier_id]:
            cached = self._instances.get(carrier_id)
            if cached is not None:
                return cached

            factory = self._registry.get(carrier["provider"])
            if factory is None:
                raise ValueError(
                    f"no IDidProvider factory registered for provider={carrier['provider']!r} "
                    f"(carrier id={carrier_id!r}) — see this module's docstring"
                )

            auth_token = (
                await self._secret_resolver.resolve(carrier["auth_token_ref"])
                if carrier.get("auth_token_ref") else None
            )
            instance = await factory(carrier, auth_token)
            self._instances[carrier_id] = instance
            return instance

    def cached_ids(self) -> frozenset[str]:
        return frozenset(self._instances.keys())
