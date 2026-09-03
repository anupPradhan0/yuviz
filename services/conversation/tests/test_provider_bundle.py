import pytest

from libs.config_sdk import ProviderConfig as SDKProviderConfig
from libs.config_sdk import ProviderConfigs

from services.conversation.provider_bundle import ProviderRegistry
from services.conversation.providers.llm.retry import RetryOnceLLM


def _sdk_cfg(id_, role, engine, model=None):
    return SDKProviderConfig(id=id_, role=role, engine=engine, model=model, voice=None, language=None, api_key_ref=None)


class _FakeManager:
    def __init__(self):
        self.requested_ids = []

    async def get(self, cfg):
        self.requested_ids.append(cfg.id)
        return f"instance:{cfg.id}"


@pytest.mark.asyncio
async def test_resolve_wraps_llm_in_retry_once():
    manager = _FakeManager()
    registry = ProviderRegistry(manager)
    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "deepgram"),
        llm=_sdk_cfg("llm1", "llm", "groq"),
        tts=_sdk_cfg("tts1", "tts", "elevenlabs"),
    )

    bundle = await registry.resolve(providers)

    assert isinstance(bundle.llm, RetryOnceLLM)
    assert bundle.llm._llm == "instance:llm1"
