import pytest

from libs.config_sdk import ProviderConfig as SDKProviderConfig
from libs.config_sdk import ProviderConfigs

from services.conversation.provider_bundle import ProviderRegistry
from services.conversation.providers.llm.fallback import FallbackLLM


def _sdk_cfg(id_, role, engine, model=None):
    return SDKProviderConfig(id=id_, role=role, engine=engine, model=model, voice=None, language=None, api_key_ref=None)


class _FakeManager:
    def __init__(self):
        self.requested_ids = []

    async def get(self, cfg):
        self.requested_ids.append(cfg.id)
        return f"instance:{cfg.id}"


@pytest.mark.asyncio
async def test_resolve_without_fallback_returns_bare_llm_instance():
    manager = _FakeManager()
    registry = ProviderRegistry(manager)
    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "deepgram"),
        llm=_sdk_cfg("llm1", "llm", "groq"),
        tts=_sdk_cfg("tts1", "tts", "elevenlabs"),
    )

    bundle = await registry.resolve(providers)

    assert bundle.llm == "instance:llm1"
    assert not isinstance(bundle.llm, FallbackLLM)


@pytest.mark.asyncio
async def test_resolve_with_fallback_wraps_llm_in_fallback_llm():
    manager = _FakeManager()
    registry = ProviderRegistry(manager)
    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "deepgram"),
        llm=_sdk_cfg("llm1", "llm", "groq"),
        tts=_sdk_cfg("tts1", "tts", "elevenlabs"),
        llm_fallback=_sdk_cfg("llm2", "llm", "gemini"),
    )

    bundle = await registry.resolve(providers)

    assert isinstance(bundle.llm, FallbackLLM)
    assert "llm2" in manager.requested_ids
