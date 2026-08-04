"""
ProviderBundle / ProviderRegistry — the boundary between "resolved config"
(libs.config_sdk.ProviderConfigs, raw rows) and "live provider instances"
(ISTT/ILLM/ITTS). Deliberately NOT part of the Config SDK: the SDK must
never import provider interface types (that would make libs/config_sdk
depend on services/conversation, inverting the dependency direction this
whole design exists to enforce). ProviderRegistry composes the existing
AIProviderManager (secret resolution, per-config-id caching, concurrency
locks — all unchanged) rather than reimplementing any of that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from libs.config_sdk import ProviderConfig as SDKProviderConfig
from libs.config_sdk import ProviderConfigs

from .ai_provider_manager import AIProviderManager, ProviderConfig


@dataclass(frozen=True)
class ProviderBundle:
    """Live, ready-to-use provider instances for one call — what
    PipelineConversationHandler actually holds and calls .transcribe()/
    .generate()/.synthesize() on."""
    stt: Any
    llm: Any
    tts: Any


def _to_ai_provider_config(cfg: SDKProviderConfig) -> ProviderConfig:
    """SDK's own ProviderConfig -> AIProviderManager's ProviderConfig. Two
    distinct types on purpose: the SDK's model is a public, stable contract
    for any consumer, AIProviderManager's is a narrower internal shape (see
    ai_provider_manager.py's own docstring on that dataclass) — a
    conversion function here is cheaper than making one type serve both
    roles and coupling their evolution together."""
    return ProviderConfig(
        id=cfg.id,
        role=cfg.role,
        engine=cfg.engine,
        model=cfg.model,
        voice=cfg.voice,
        language=cfg.language,
        api_key_ref=cfg.api_key_ref,
        extra=cfg.extra,
    )


class ProviderRegistry:
    """Thin composition wrapper around AIProviderManager, giving
    resolve(providers) the one-call shape agent_resolver.py needs — turns
    RuntimeConfig.providers (raw ProviderConfig rows) into a ProviderBundle
    of live instances, reusing AIProviderManager's existing caching/secret-
    resolution/concurrency behavior unchanged."""

    def __init__(self, manager: AIProviderManager) -> None:
        self._manager = manager

    async def resolve(self, providers: ProviderConfigs) -> ProviderBundle:
        stt = await self._manager.get(_to_ai_provider_config(providers.stt))
        llm = await self._manager.get(_to_ai_provider_config(providers.llm))
        tts = await self._manager.get(_to_ai_provider_config(providers.tts))
        return ProviderBundle(stt=stt, llm=llm, tts=tts)
