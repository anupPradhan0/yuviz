"""
Logic tests for AIProviderManager use an injected fake registry — caching,
concurrency, and secret-resolution-timing are properties of the manager
itself, independent of which real engine is behind it. Real-engine tests
(faster_whisper/ollama/macos/kokoro actually instantiating) are run manually,
not committed here — faster_whisper's model load alone takes ~13s even for
the "tiny" model, which would make every `pytest` run pay that cost.

The two exceptions below (Ollama, macOS TTS) ARE committed: neither one's
constructor makes a network call or loads a model file, so they're as fast
as the fakes and worth proving end-to-end against real code.
"""

from __future__ import annotations

import asyncio

import pytest

from ..ai_provider_manager import AIProviderManager, ProviderConfig


class FakeSecretResolver:
    def __init__(self) -> None:
        self.resolved_refs: list[str] = []

    async def resolve(self, ref: str) -> str:
        self.resolved_refs.append(ref)
        return f"resolved:{ref}"


class FakeProviderInstance:
    def __init__(self, cfg: ProviderConfig, api_key: str | None) -> None:
        self.cfg = cfg
        self.api_key = api_key


async def _fake_factory(cfg: ProviderConfig, api_key: str | None) -> FakeProviderInstance:
    return FakeProviderInstance(cfg, api_key)


FAKE_REGISTRY = {("stt", "fake_engine"): _fake_factory}


class TestCachingAndInstantiation:
    async def test_get_creates_instance_on_first_call(self):
        manager = AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY)
        cfg = ProviderConfig(id="p1", role="stt", engine="fake_engine")

        instance = await manager.get(cfg)
        assert isinstance(instance, FakeProviderInstance)
        assert instance.cfg is cfg
        assert manager.cached_ids() == {"p1"}

    async def test_get_returns_same_instance_on_second_call(self):
        manager = AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY)
        cfg = ProviderConfig(id="p1", role="stt", engine="fake_engine")

        first = await manager.get(cfg)
        second = await manager.get(cfg)
        assert first is second

    async def test_distinct_configs_get_distinct_instances(self):
        manager = AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY)
        cfg_a = ProviderConfig(id="p1", role="stt", engine="fake_engine")
        cfg_b = ProviderConfig(id="p2", role="stt", engine="fake_engine")

        a = await manager.get(cfg_a)
        b = await manager.get(cfg_b)
        assert a is not b
        assert manager.cached_ids() == {"p1", "p2"}

    async def test_unregistered_engine_raises_value_error(self):
        manager = AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY)
        cfg = ProviderConfig(id="p1", role="stt", engine="does_not_exist")
        with pytest.raises(ValueError):
            await manager.get(cfg)

    async def test_get_stt_asserts_role_matches(self):
        manager = AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY)
        wrong_role_cfg = ProviderConfig(id="p1", role="llm", engine="fake_engine")
        with pytest.raises(AssertionError):
            await manager.get_stt(wrong_role_cfg)


class TestSecretResolution:
    async def test_api_key_ref_is_resolved_and_passed_to_factory(self):
        resolver = FakeSecretResolver()
        manager = AIProviderManager(resolver, registry=FAKE_REGISTRY)
        cfg = ProviderConfig(
            id="p1", role="stt", engine="fake_engine", api_key_ref="env:FAKE_KEY",
        )

        instance = await manager.get(cfg)
        assert instance.api_key == "resolved:env:FAKE_KEY"
        assert resolver.resolved_refs == ["env:FAKE_KEY"]

    async def test_no_api_key_ref_means_no_resolution_call(self):
        resolver = FakeSecretResolver()
        manager = AIProviderManager(resolver, registry=FAKE_REGISTRY)
        cfg = ProviderConfig(id="p1", role="stt", engine="fake_engine")  # no api_key_ref

        instance = await manager.get(cfg)
        assert instance.api_key is None
        assert resolver.resolved_refs == []

    async def test_secret_resolved_exactly_once_across_repeated_get_calls(self):
        """The non-negotiable rule: secrets resolve at instantiation, never
        per-call — a cached provider must not re-trigger resolution."""
        resolver = FakeSecretResolver()
        manager = AIProviderManager(resolver, registry=FAKE_REGISTRY)
        cfg = ProviderConfig(
            id="p1", role="stt", engine="fake_engine", api_key_ref="env:FAKE_KEY",
        )

        await manager.get(cfg)
        await manager.get(cfg)
        await manager.get(cfg)
        assert resolver.resolved_refs == ["env:FAKE_KEY"]  # exactly once, not three times


class TestConcurrency:
    async def test_concurrent_get_for_same_config_creates_only_one_instance(self):
        call_count = 0

        async def slow_factory(cfg: ProviderConfig, api_key: str | None) -> FakeProviderInstance:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)  # simulate slow instantiation (model load)
            return FakeProviderInstance(cfg, api_key)

        manager = AIProviderManager(
            FakeSecretResolver(), registry={("stt", "slow"): slow_factory},
        )
        cfg = ProviderConfig(id="p1", role="stt", engine="slow")

        results = await asyncio.gather(*(manager.get(cfg) for _ in range(10)))
        assert call_count == 1
        assert all(r is results[0] for r in results)

    async def test_concurrent_get_for_different_configs_does_not_serialize(self):
        """Per-config-id locking: instantiating config A must not block a
        concurrent request for already-cached config B."""
        async def slow_factory(cfg, api_key):
            await asyncio.sleep(0.2)
            return FakeProviderInstance(cfg, api_key)

        registry = {**FAKE_REGISTRY, ("stt", "slow_engine"): slow_factory}
        manager = AIProviderManager(FakeSecretResolver(), registry=registry)
        cfg_a = ProviderConfig(id="a", role="stt", engine="slow_engine")
        cfg_b = ProviderConfig(id="b", role="stt", engine="fake_engine")
        await manager.get(cfg_b)  # warm b's cache

        loop = asyncio.get_running_loop()
        start = loop.time()
        task_a = asyncio.create_task(manager.get(cfg_a))
        await asyncio.sleep(0.01)  # let task_a acquire cfg_a's lock first
        b_result = await manager.get(cfg_b)  # must return immediately from cache
        elapsed_for_b = loop.time() - start
        await task_a

        assert elapsed_for_b < 0.15  # nowhere near cfg_a's 0.2s instantiation
        assert b_result.cfg is cfg_b


class TestInvalidate:
    async def test_invalidate_removes_cached_instance(self):
        manager = AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY)
        cfg = ProviderConfig(id="p1", role="stt", engine="fake_engine")
        await manager.get(cfg)
        assert manager.cached_ids() == {"p1"}

        evicted = manager.invalidate("p1")

        assert evicted is True
        assert manager.cached_ids() == frozenset()

    async def test_get_after_invalidate_reconstructs_a_new_instance(self):
        manager = AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY)
        cfg = ProviderConfig(id="p1", role="stt", engine="fake_engine")
        first = await manager.get(cfg)

        manager.invalidate("p1")
        second = await manager.get(cfg)

        assert second is not first  # a genuinely new instance, not the stale cached one

    async def test_invalidate_unknown_id_returns_false_and_does_not_raise(self):
        manager = AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY)
        assert manager.invalidate("never-cached") is False

    async def test_invalidate_does_not_affect_other_cached_configs(self):
        manager = AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY)
        cfg_a = ProviderConfig(id="p1", role="stt", engine="fake_engine")
        cfg_b = ProviderConfig(id="p2", role="stt", engine="fake_engine")
        await manager.get(cfg_a)
        b = await manager.get(cfg_b)

        manager.invalidate("p1")

        assert manager.cached_ids() == {"p2"}
        assert await manager.get(cfg_b) is b  # untouched, still the same instance


class TestPrewarm:
    async def test_prewarm_instantiates_all_given_configs(self):
        manager = AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY)
        configs = [
            ProviderConfig(id="p1", role="stt", engine="fake_engine"),
            ProviderConfig(id="p2", role="stt", engine="fake_engine"),
        ]
        await manager.prewarm(configs)
        assert manager.cached_ids() == {"p1", "p2"}


# ── Real-engine tests — Ollama and macOS TTS only (see module docstring) ────

class TestRealOllamaFactory:
    async def test_get_llm_creates_real_ollama_instance(self):
        manager = AIProviderManager(FakeSecretResolver())  # real default registry
        cfg = ProviderConfig(id="llm-1", role="llm", engine="ollama", model="llama3.2")

        instance = await manager.get_llm(cfg)
        assert type(instance).__name__ == "OllamaLLM"

    async def test_real_ollama_generates_a_token_stream(self):
        from ..providers.interfaces import ChatMessage

        manager = AIProviderManager(FakeSecretResolver())
        cfg = ProviderConfig(id="llm-1", role="llm", engine="ollama", model="llama3.2")
        llm = await manager.get_llm(cfg)

        tokens = []
        async for tok in llm.generate([ChatMessage(role="user", content="Say hi in 2 words.")]):
            tokens.append(tok)
        assert "".join(tokens).strip() != ""


class TestRealMacosTtsFactory:
    async def test_get_tts_creates_real_macos_instance_and_synthesizes_audio(self):
        manager = AIProviderManager(FakeSecretResolver())
        cfg = ProviderConfig(id="tts-1", role="tts", engine="macos", voice="Samantha")

        tts = await manager.get_tts(cfg)
        assert type(tts).__name__ == "MacOSTTS"

        audio = await tts.synthesize("Testing.", sample_rate=16_000)
        assert len(audio) > 0


# ── Cloud engine registry — construction only, no network (see
# test_deepgram.py/test_openai_llm.py/test_elevenlabs.py for behavior tests
# against a mocked transport) ────────────────────────────────────────────────

class TestCloudEngineRegistry:
    async def test_get_stt_creates_deepgram_instance(self):
        manager = AIProviderManager(FakeSecretResolver())
        cfg = ProviderConfig(id="stt-1", role="stt", engine="deepgram", api_key_ref="k8s:x/deepgram")

        instance = await manager.get_stt(cfg)
        assert type(instance).__name__ == "DeepgramSTT"

    async def test_get_llm_creates_openai_instance(self):
        manager = AIProviderManager(FakeSecretResolver())
        cfg = ProviderConfig(id="llm-2", role="llm", engine="openai", api_key_ref="k8s:x/openai")

        instance = await manager.get_llm(cfg)
        assert type(instance).__name__ == "OpenAILLM"

    async def test_get_tts_creates_elevenlabs_instance(self):
        manager = AIProviderManager(FakeSecretResolver())
        cfg = ProviderConfig(
            id="tts-2", role="tts", engine="elevenlabs",
            voice="voice-123", api_key_ref="k8s:x/elevenlabs",
        )

        instance = await manager.get_tts(cfg)
        assert type(instance).__name__ == "ElevenLabsTTS"

    async def test_get_llm_creates_anthropic_instance(self):
        manager = AIProviderManager(FakeSecretResolver())
        cfg = ProviderConfig(id="llm-3", role="llm", engine="anthropic", api_key_ref="k8s:x/anthropic")

        instance = await manager.get_llm(cfg)
        assert type(instance).__name__ == "AnthropicLLM"

    # All three are OpenAILLM at a different base_url, so the endpoint is
    # what's worth asserting — a class-name check would pass even if a
    # factory sent Cohere's traffic to OpenAI.
    async def test_openai_compatible_engines_get_their_own_base_url(self):
        manager = AIProviderManager(FakeSecretResolver())
        expected = {
            "groq":   "https://api.groq.com/openai",
            "nvidia": "https://integrate.api.nvidia.com",
            "cohere": "https://api.cohere.ai/compatibility",
            "openai": "https://api.openai.com",
        }
        for engine, base_url in expected.items():
            cfg = ProviderConfig(
                id=f"llm-{engine}", role="llm", engine=engine, api_key_ref=f"k8s:x/{engine}",
            )
            instance = await manager.get_llm(cfg)
            assert type(instance).__name__ == "OpenAILLM"
            assert str(instance._client.base_url).rstrip("/") == base_url

    # Cheap-by-default is deliberate, not an accident of ordering.
    async def test_llm_engines_default_to_their_cheap_model(self):
        manager = AIProviderManager(FakeSecretResolver())
        expected = {
            "openai":    "gpt-4o-mini",
            "anthropic": "claude-haiku-4-5",
            "nvidia":    "meta/llama-3.1-8b-instruct",
            "cohere":    "command-r7b-12-2024",
        }
        for engine, model in expected.items():
            cfg = ProviderConfig(
                id=f"cheap-{engine}", role="llm", engine=engine, api_key_ref=f"k8s:x/{engine}",
            )  # no model set — the factory default is what's under test
            instance = await manager.get_llm(cfg)
            assert instance._model == model

    async def test_cloud_engine_without_api_key_ref_raises_value_error(self):
        manager = AIProviderManager(FakeSecretResolver())
        cfg = ProviderConfig(id="stt-3", role="stt", engine="deepgram")  # no api_key_ref

        with pytest.raises(ValueError, match="no api_key_ref configured"):
            await manager.get_stt(cfg)
