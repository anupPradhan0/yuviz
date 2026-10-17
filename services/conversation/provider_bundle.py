"""
Turns resolved provider config rows into live ISTT/ILLM/ITTS instances.
Lives here, not in libs/config_sdk, so the SDK never depends on services/conversation.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from libs.config_sdk import ProviderConfig as SDKProviderConfig
from libs.config_sdk import ProviderConfigs
from libs.config_sdk.languages import tts_languages, tts_model_of

from .ai_provider_manager import AIProviderManager, ModelNotReadyError, ProviderConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderBundle:
    """Live provider instances for one call."""
    stt: Any
    llm: Any
    tts: Any
    # Per-language voice overrides (multilingual agents); absent language = `tts`.
    tts_by_language: dict[str, Any] = field(default_factory=dict)

    def tts_for(self, language: str | None) -> Any:
        return self.tts_by_language.get(language, self.tts) if language else self.tts


def _to_ai_provider_config(cfg: SDKProviderConfig) -> ProviderConfig:
    """Convert the SDK's public ProviderConfig to AIProviderManager's internal one."""
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
    """Resolves ProviderConfigs into a ProviderBundle via AIProviderManager's cache."""

    def __init__(self, manager: AIProviderManager) -> None:
        self._manager = manager
        self._default_bundle: ProviderBundle | None = None
        self._default_stt_languages: frozenset[str] = frozenset()
        self._default_tts_languages: frozenset[str] = frozenset()

    def set_defaults(
        self, bundle: ProviderBundle, *, stt_languages: frozenset[str], tts_languages: frozenset[str],
    ) -> None:
        """The process-default providers and the languages the STT and TTS among them serve; a
        role whose own local model isn't on disk yet can stand in with its default."""
        self._default_bundle = bundle
        self._default_stt_languages = stt_languages
        self._default_tts_languages = tts_languages

    async def resolve(self, providers: ProviderConfigs, languages: tuple[str, ...]) -> ProviderBundle:
        """`languages` are the agent's: a stand-in default is used only where it serves them."""
        from .providers.llm.retry import RetryOnceLLM

        default = self._default_bundle
        wanted = frozenset(languages)
        stt = await self._get_or_default(
            "stt", providers.stt, default and default.stt, wanted <= self._default_stt_languages,
        )
        llm = await self._get_or_default("llm", providers.llm, default and default.llm, True)
        tts = await self._get_or_default(
            "tts", providers.tts, default and default.tts, wanted <= self._default_tts_languages,
        )
        llm = RetryOnceLLM(llm, name=f"{providers.llm.engine}:{providers.llm.model}")
        # Overrides were tenant-checked by the Config SDK; instances are cached per
        # provider_configs.id like any other, so this is call setup, never a turn.
        overrides = getattr(providers, "tts_by_language", None) or {}
        tts_by_language: dict[str, Any] = {}
        if overrides:
            base_languages = (
                self._default_tts_languages if default is not None and tts is default.tts
                else tts_languages(
                    providers.tts.engine, tts_model_of(providers.tts.engine, providers.tts.model, providers.tts.extra),
                    providers.tts.voice,
                )
            )
            langs = list(overrides)
            instances = await asyncio.gather(
                *(self._manager.get(_to_ai_provider_config(overrides[lang])) for lang in langs),
                return_exceptions=True,
            )
            for lang, inst in zip(langs, instances):
                if isinstance(inst, ModelNotReadyError):
                    if lang in base_languages:
                        log.warning("%s — %s uses the agent's base voice", inst, lang)
                    elif default is not None and lang in self._default_tts_languages:
                        log.warning("%s — %s uses the default voice", inst, lang)
                        tts_by_language[lang] = default.tts
                    elif inst.provider is not None:
                        # Neither shared voice speaks it: use the override itself, which loads (and
                        # downloads) on its first synthesis, off the loop.
                        log.warning("%s — %s loads its own voice on first use", inst, lang)
                        tts_by_language[lang] = inst.provider
                    else:
                        log.error(
                            "%s, and neither the agent's base voice nor the default voice speaks %s — "
                            "%s will be spoken with the base voice's pronunciation",
                            inst, lang, lang,
                        )
                    continue
                if isinstance(inst, BaseException):
                    # A broken override must not fail the call; that language uses the base voice.
                    log.error(
                        "TTS override for %s (provider_config %s) failed to load — using the base voice",
                        lang, overrides[lang].id, exc_info=inst,
                    )
                    continue
                tts_by_language[lang] = inst
        return ProviderBundle(stt=stt, llm=llm, tts=tts, tts_by_language=tts_by_language)

    async def _get_or_default(
        self, role: str, cfg: SDKProviderConfig, default: Any, default_serves: bool,
    ) -> Any:
        """One role's provider. When its local model isn't on disk: the process default for that
        role if it serves the agent, else the not-ready instance itself (it loads or fails lazily,
        as before). Other roles are never touched."""
        try:
            return await self._manager.get(_to_ai_provider_config(cfg))
        except ModelNotReadyError as exc:
            if default is not None and default_serves:
                log.warning("%s — this call uses the default %s", exc, role)
                if (aclose := getattr(exc.provider, "aclose", None)) is not None:
                    await aclose()  # a not-ready Ollama client that will never be used
                return default
            if exc.provider is None:
                raise
            log.error("%s and the default %s can't serve this agent — using it as it is", exc, role)
            return exc.provider
