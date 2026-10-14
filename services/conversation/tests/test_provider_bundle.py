import pytest

from libs.config_sdk import ProviderConfig as SDKProviderConfig
from libs.config_sdk import ProviderConfigs

from services.conversation.ai_provider_manager import ModelNotReadyError
from services.conversation.provider_bundle import ProviderBundle, ProviderRegistry
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

    bundle = await registry.resolve(providers, ("en",))

    assert isinstance(bundle.llm, RetryOnceLLM)
    assert bundle.llm._llm == "instance:llm1"


@pytest.mark.asyncio
async def test_resolve_includes_per_language_tts_overrides():
    manager = _FakeManager()
    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "deepgram"),
        llm=_sdk_cfg("llm1", "llm", "openai"),
        tts=_sdk_cfg("tts1", "tts", "cartesia"),
        tts_by_language={"hi": _sdk_cfg("tts-hi", "tts", "cartesia")},
    )

    bundle = await ProviderRegistry(manager).resolve(providers, ("en",))

    assert bundle.tts_for("hi") == "instance:tts-hi"
    assert bundle.tts_for("en") == "instance:tts1"
    assert bundle.tts_for(None) == "instance:tts1"


@pytest.mark.asyncio
async def test_broken_override_falls_back_to_base_voice():
    class _Failing(_FakeManager):
        async def get(self, cfg):
            if cfg.id == "tts-hi":
                raise ValueError("no api key")
            return await super().get(cfg)

    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "deepgram"),
        llm=_sdk_cfg("llm1", "llm", "openai"),
        tts=_sdk_cfg("tts1", "tts", "cartesia"),
        tts_by_language={"hi": _sdk_cfg("tts-hi", "tts", "cartesia")},
    )
    bundle = await ProviderRegistry(_Failing()).resolve(providers, ("en",))
    assert bundle.tts_for("hi") == "instance:tts1"


class _NotReady(_FakeManager):
    """Raises ModelNotReadyError for the given config ids, carrying a not-ready instance."""

    def __init__(self, *not_ready_ids):
        super().__init__()
        self._not_ready_ids = not_ready_ids

    async def get(self, cfg):
        if cfg.id in self._not_ready_ids:
            raise ModelNotReadyError(cfg.engine, "model", provider=f"not-ready:{cfg.id}")
        return await super().get(cfg)


def _defaults(registry, *, stt=frozenset({"en"}), tts=frozenset({"en"})):
    registry.set_defaults(
        ProviderBundle(stt="default-stt", llm="default-llm", tts="default-tts"),
        stt_languages=stt, tts_languages=tts,
    )
    return registry


@pytest.mark.asyncio
async def test_a_not_ready_override_keeps_a_base_voice_that_speaks_the_language():
    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "deepgram"),
        llm=_sdk_cfg("llm1", "llm", "openai"),
        tts=_sdk_cfg("tts1", "tts", "elevenlabs"),
        tts_by_language={"hi": _sdk_cfg("tts-hi", "tts", "kokoro")},
    )

    bundle = await _defaults(ProviderRegistry(_NotReady("tts-hi"))).resolve(providers, ("en", "hi"))

    assert bundle.tts_for("hi") == "instance:tts1"  # ElevenLabs multilingual, not the English default


@pytest.mark.asyncio
async def test_a_not_ready_override_uses_the_default_voice_only_if_it_speaks_the_language():
    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "deepgram"),
        llm=_sdk_cfg("llm1", "llm", "openai"),
        tts=_sdk_cfg("tts1", "tts", "kokoro"),  # default voice af_sarah: English only
        tts_by_language={"hi": _sdk_cfg("tts-hi", "tts", "kokoro")},
    )

    spoken = await _defaults(ProviderRegistry(_NotReady("tts-hi")), tts=frozenset({"en", "hi"})).resolve(
        providers, ("en", "hi"),
    )
    skipped = await _defaults(ProviderRegistry(_NotReady("tts-hi"))).resolve(providers, ("en", "hi"))

    assert spoken.tts_for("hi") == "default-tts"
    # Nobody else speaks it: the override itself, loading on first use; the call is not failed.
    assert skipped.tts_for("hi") == "not-ready:tts-hi"


@pytest.mark.asyncio
async def test_only_the_not_ready_role_is_replaced():
    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "deepgram"),
        llm=_sdk_cfg("llm1", "llm", "ollama", "qwen3:4b"),
        tts=_sdk_cfg("tts1", "tts", "elevenlabs"),
    )

    bundle = await _defaults(ProviderRegistry(_NotReady("llm1"))).resolve(providers, ("hi",))

    assert (bundle.stt, bundle.tts) == ("instance:stt1", "instance:tts1")
    assert bundle.llm._llm == "default-llm"  # the LLM is language-agnostic


@pytest.mark.asyncio
async def test_a_default_that_cannot_serve_the_agent_is_not_used():
    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "faster_whisper", "small"),
        llm=_sdk_cfg("llm1", "llm", "openai"),
        tts=_sdk_cfg("tts1", "tts", "cartesia"),
    )

    bundle = await _defaults(ProviderRegistry(_NotReady("stt1"))).resolve(providers, ("hi",))

    # The English default can't hear Hindi: the agent's own (not-ready) STT is kept, never rejected.
    assert bundle.stt == "not-ready:stt1"
    assert bundle.tts == "instance:tts1"


@pytest.mark.asyncio
async def test_model_not_ready_without_a_built_instance_propagates():
    class _Bare(_FakeManager):
        async def get(self, cfg):
            raise ModelNotReadyError("kokoro", "base")

    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "deepgram"), llm=_sdk_cfg("llm1", "llm", "openai"),
        tts=_sdk_cfg("tts1", "tts", "kokoro"),
    )
    with pytest.raises(ModelNotReadyError):
        await ProviderRegistry(_Bare()).resolve(providers, ("en",))


@pytest.mark.asyncio
async def test_an_unused_not_ready_instance_is_closed():
    closed = []

    class _Closable:
        async def aclose(self):
            closed.append(True)

    class _Manager(_FakeManager):
        async def get(self, cfg):
            if cfg.id == "llm1":
                raise ModelNotReadyError("ollama", "qwen3:4b", provider=_Closable())
            return await super().get(cfg)

    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "deepgram"), llm=_sdk_cfg("llm1", "llm", "ollama", "qwen3:4b"),
        tts=_sdk_cfg("tts1", "tts", "cartesia"),
    )

    await _defaults(ProviderRegistry(_Manager())).resolve(providers, ("en",))

    assert closed == [True]


@pytest.mark.asyncio
async def test_a_not_ready_whisper_keeps_downloading_while_the_default_stands_in():
    stopped = []

    class _DownloadingWhisper:
        # Shared and still fetching its model: the bundle's cleanup of unused clients must not stop it.
        async def stop_download(self):
            stopped.append(True)

    class _Manager(_FakeManager):
        async def get(self, cfg):
            if cfg.id == "stt1":
                raise ModelNotReadyError("faster_whisper", "small", provider=_DownloadingWhisper())
            return await super().get(cfg)

    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "faster_whisper", "small"), llm=_sdk_cfg("llm1", "llm", "ollama"),
        tts=_sdk_cfg("tts1", "tts", "cartesia"),
    )

    bundle = await _defaults(ProviderRegistry(_Manager())).resolve(providers, ("en",))

    assert bundle.stt == "default-stt" and stopped == []


@pytest.mark.asyncio
async def test_a_default_stt_pinned_to_english_does_not_stand_in_for_a_hindi_agent():
    from types import SimpleNamespace

    from services.conversation.__main__ import _default_stt_languages
    from services.conversation.pipeline_config import SttConfig

    pinned = SimpleNamespace(stt=SttConfig(model_size="small", language="en"))
    detecting = SimpleNamespace(stt=SttConfig(model_size="small", language=None))
    assert _default_stt_languages(pinned) == frozenset({"en"})
    assert "hi" in _default_stt_languages(detecting)

    providers = ProviderConfigs(
        stt=_sdk_cfg("stt1", "stt", "faster_whisper", "small"),
        llm=_sdk_cfg("llm1", "llm", "openai"),
        tts=_sdk_cfg("tts1", "tts", "cartesia"),
    )
    registry = ProviderRegistry(_NotReady("stt1"))
    registry.set_defaults(
        ProviderBundle(stt="default-stt", llm="default-llm", tts="default-tts"),
        stt_languages=_default_stt_languages(pinned), tts_languages=frozenset({"en"}),
    )

    bundle = await registry.resolve(providers, ("hi",))

    assert bundle.stt == "not-ready:stt1"
