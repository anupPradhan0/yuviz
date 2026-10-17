"""
AIProviderManager logic tests via a fake registry. Real engines are only tested
where construction is cheap (Ollama, macOS TTS); model-loading engines are too slow.
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest

from huggingface_hub.errors import LocalEntryNotFoundError

from .. import ai_provider_manager, model_downloads
from ..ai_provider_manager import (
    close_not_ready_providers,
    AIProviderManager,
    ModelNotReadyError,
    ProviderConfig,
    _make_faster_whisper,
    _make_kokoro_tts,
    _make_ollama,
)


@pytest.fixture(autouse=True)
def _no_not_ready_instances_carried_between_tests():
    ai_provider_manager._not_ready_whisper.clear()
    ai_provider_manager._not_ready_kokoro.clear()
    model_downloads._done.clear()
    yield
    ai_provider_manager._not_ready_whisper.clear()
    ai_provider_manager._not_ready_kokoro.clear()
    model_downloads._done.clear()


@pytest.fixture(autouse=True)
def _ollama_model_is_pulled(monkeypatch):
    """Keep Ollama factory tests off the network; a test can re-patch it to say "not pulled"."""
    from ..providers.llm.ollama import OllamaLLM

    async def pulled(self):
        return True

    monkeypatch.setattr(OllamaLLM, "model_pulled", pulled)


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
        """Secrets resolve at instantiation only, never per get()."""
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
        """Instantiating config A must not block a get() for cached config B."""
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

async def _first_available_ollama_model() -> str | None:
    try:
        async with httpx.AsyncClient(base_url="http://localhost:11434", timeout=5.0) as client:
            resp = await client.get("/api/tags")
            resp.raise_for_status()
            models = resp.json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return None
    # /api/tags order is arbitrary and embedding-only models reject /api/chat.
    chat = [m["name"] for m in models if "embed" not in m["name"]]
    return chat[0] if chat else None


class TestRealOllamaFactory:
    async def test_get_llm_creates_real_ollama_instance(self):
        model = await _first_available_ollama_model()
        if model is None:
            pytest.skip("no Ollama models available locally")
        manager = AIProviderManager(FakeSecretResolver())  # real default registry
        cfg = ProviderConfig(id="llm-1", role="llm", engine="ollama", model=model)

        instance = await manager.get_llm(cfg)
        assert type(instance).__name__ == "OllamaLLM"

    async def test_real_ollama_generates_a_token_stream(self):
        from ..providers.interfaces import ChatMessage

        model = await _first_available_ollama_model()
        if model is None:
            pytest.skip("no Ollama models available locally")
        manager = AIProviderManager(FakeSecretResolver())
        cfg = ProviderConfig(id="llm-1", role="llm", engine="ollama", model=model)
        llm = await manager.get_llm(cfg)

        tokens = []
        async for tok in llm.generate([ChatMessage(role="user", content="Say hi in 2 words.")]):
            tokens.append(tok)
        assert "".join(tokens).strip() != ""


class TestOllamaThinkResolution:
    async def _mock_llm(self, cfg: ProviderConfig, handler):
        llm = await _make_ollama(cfg, None)
        llm._client = httpx.AsyncClient(base_url="http://localhost:11434", transport=httpx.MockTransport(handler))
        return llm

    async def test_think_true_reaches_generate(self):
        from ..providers.interfaces import ChatMessage

        seen_payload = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_payload.update(json.loads(request.content))
            body = b'{"message": {"role": "assistant", "content": "ok"}, "done": true}\n'
            return httpx.Response(200, content=body)

        cfg = ProviderConfig(id="llm-1", role="llm", engine="ollama", extra={"think": True})
        llm = await self._mock_llm(cfg, handler)
        _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

        assert seen_payload["think"] is True

    async def test_think_true_reaches_generate_with_tools(self):
        from ..providers.interfaces import ChatMessage

        seen_payload = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_payload.update(json.loads(request.content))
            body = b'{"message": {"role": "assistant", "content": "ok"}, "done": true}\n'
            return httpx.Response(200, content=body)

        cfg = ProviderConfig(id="llm-1", role="llm", engine="ollama", extra={"think": True})
        llm = await self._mock_llm(cfg, handler)
        schemas = [{"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}}]
        _ = [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="hi")], schemas)]

        assert seen_payload["think"] is True
        assert seen_payload["tools"] == [{"type": "function", "function": schemas[0]}]

    async def test_think_false_reaches_generate_with_no_warning(self, caplog):
        from ..providers.interfaces import ChatMessage

        seen_payload = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_payload.update(json.loads(request.content))
            body = b'{"message": {"role": "assistant", "content": "ok"}, "done": true}\n'
            return httpx.Response(200, content=body)

        cfg = ProviderConfig(id="llm-1", role="llm", engine="ollama", extra={"think": False})
        with caplog.at_level("WARNING"):
            llm = await self._mock_llm(cfg, handler)
            _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

        assert seen_payload["think"] is False
        assert not any("extra.think" in record.message for record in caplog.records)

    async def test_no_think_key_when_extra_is_empty(self):
        from ..providers.interfaces import ChatMessage

        seen_payload = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_payload.update(json.loads(request.content))
            body = b'{"message": {"role": "assistant", "content": "ok"}, "done": true}\n'
            return httpx.Response(200, content=body)

        cfg = ProviderConfig(id="llm-1", role="llm", engine="ollama")
        llm = await self._mock_llm(cfg, handler)
        _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

        assert "think" not in seen_payload

    @pytest.mark.parametrize("bad_value", ["yes", 1, None, []])
    async def test_non_bool_think_falls_back_and_warns(self, bad_value, caplog):
        from ..providers.interfaces import ChatMessage

        seen_payload = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_payload.update(json.loads(request.content))
            body = b'{"message": {"role": "assistant", "content": "ok"}, "done": true}\n'
            return httpx.Response(200, content=body)

        cfg = ProviderConfig(id="llm-1", role="llm", engine="ollama", extra={"think": bad_value})
        with caplog.at_level("WARNING"):
            llm = await self._mock_llm(cfg, handler)
            _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

        assert "think" not in seen_payload
        assert any("extra.think" in record.message for record in caplog.records)

    async def test_thinking_capable_model_defaults_think_false_not_omitted(self):
        """Thinking-capable models get think=False when unset (the UI can't set extra.think)."""
        from ..providers.interfaces import ChatMessage

        seen_payload = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_payload.update(json.loads(request.content))
            body = b'{"message": {"role": "assistant", "content": "ok"}, "done": true}\n'
            return httpx.Response(200, content=body)

        cfg = ProviderConfig(id="llm-1", role="llm", engine="ollama", model="gemma4:e2b")
        llm = await self._mock_llm(cfg, handler)
        _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

        assert seen_payload["think"] is False

    async def test_thinking_capable_model_explicit_true_still_honored(self):
        from ..providers.interfaces import ChatMessage

        seen_payload = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_payload.update(json.loads(request.content))
            body = b'{"message": {"role": "assistant", "content": "ok"}, "done": true}\n'
            return httpx.Response(200, content=body)

        cfg = ProviderConfig(
            id="llm-1", role="llm", engine="ollama", model="gemma4:e2b", extra={"think": True},
        )
        llm = await self._mock_llm(cfg, handler)
        _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

        assert seen_payload["think"] is True

    @pytest.mark.parametrize("string_value,expected", [("true", True), ("false", False), ("True", True), (" False ", False)])
    async def test_string_true_false_is_parsed_not_treated_as_malformed(self, string_value, expected, caplog):
        """String booleans from form-edited configs parse rather than warn as malformed."""
        from ..providers.interfaces import ChatMessage

        seen_payload = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_payload.update(json.loads(request.content))
            body = b'{"message": {"role": "assistant", "content": "ok"}, "done": true}\n'
            return httpx.Response(200, content=body)

        cfg = ProviderConfig(id="llm-1", role="llm", engine="ollama", extra={"think": string_value})
        with caplog.at_level("WARNING"):
            llm = await self._mock_llm(cfg, handler)
            _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

        assert seen_payload["think"] is expected
        assert not any("extra.think" in record.message for record in caplog.records)


@pytest.mark.skipif(sys.platform != "darwin", reason="synthesizes with macOS's /usr/bin/say")
class TestRealMacosTtsFactory:
    async def test_get_tts_creates_real_macos_instance_and_synthesizes_audio(self):
        manager = AIProviderManager(FakeSecretResolver())
        cfg = ProviderConfig(id="tts-1", role="tts", engine="macos", voice="Samantha")

        tts = await manager.get_tts(cfg)
        assert type(tts).__name__ == "MacOSTTS"

        audio = await tts.synthesize("Testing.", sample_rate=16_000)
        assert len(audio) > 0


# ── Local models not on disk ──────────────────────────────────────────────────

class TestModelNotReady:
    async def test_kokoro_missing_base_weights_raise_carrying_the_built_instance(self, monkeypatch):
        def fake_download(repo, filename, local_files_only):
            if filename == "kokoro-v1_0.pth":
                raise LocalEntryNotFoundError("not cached")
            return "/cache/" + filename

        from ..providers.tts import kokoro as kokoro_module

        monkeypatch.setattr(ai_provider_manager, "hf_hub_download", fake_download)
        monkeypatch.setattr(kokoro_module, "KokoroTTS", lambda **kw: "unloaded-tts")
        cfg = ProviderConfig(id="tts-1", role="tts", engine="kokoro", voice="af_heart")

        with pytest.raises(ModelNotReadyError) as exc:
            await _make_kokoro_tts(cfg, None)

        # The built instance rides along for a caller with nothing better to use.
        assert (exc.value.engine, exc.value.provider) == ("kokoro", "unloaded-tts")

    async def test_kokoro_builds_nothing_heavy_while_the_weights_are_missing(self, monkeypatch):
        import sys
        import types

        built = []

        class _KPipeline:
            def __init__(self, lang_code, model=None):
                built.append(lang_code)
                self.model = object()

            def __call__(self, text, voice, speed):
                return iter(())

        def fake_download(repo, filename, local_files_only):
            if filename == "kokoro-v1_0.pth":
                raise LocalEntryNotFoundError("not cached")
            return "/cache/" + filename

        monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=_KPipeline))
        monkeypatch.setattr(ai_provider_manager, "hf_hub_download", fake_download)
        cfg = ProviderConfig(id="tts-1", role="tts", engine="kokoro", voice="af_heart")

        with pytest.raises(ModelNotReadyError) as exc:
            await _make_kokoro_tts(cfg, None)

        assert built == []  # no KPipeline/KModel: nothing downloaded on the event loop
        assert await exc.value.provider.synthesize("hello", 24_000) == b""
        assert built == ["a"]  # the first synthesis loads it (in the executor)

    async def test_concurrent_not_ready_kokoro_calls_share_one_deferred_instance(self, monkeypatch):
        def fake_download(repo, filename, local_files_only):
            raise LocalEntryNotFoundError("not cached")

        monkeypatch.setattr(ai_provider_manager, "hf_hub_download", fake_download)
        cfg = ProviderConfig(id="tts-1", role="tts", engine="kokoro", voice="hf_alpha")
        providers = []
        for _ in range(3):
            with pytest.raises(ModelNotReadyError) as exc:
                await _make_kokoro_tts(cfg, None)
            providers.append(exc.value.provider)
        assert providers[0] is providers[1] is providers[2]

    async def test_kokoro_voice_file_is_left_to_the_lazy_fetch(self, monkeypatch):
        checked = []

        def fake_download(repo, filename, local_files_only):
            checked.append(filename)
            return "/cache/" + filename

        from ..providers.tts import kokoro as kokoro_module

        constructed = []

        class _Tts:
            def __init__(self, **kw):
                constructed.append(kw)

            async def load(self):
                pass

        monkeypatch.setattr(ai_provider_manager, "hf_hub_download", fake_download)
        monkeypatch.setattr(kokoro_module, "KokoroTTS", _Tts)
        cfg = ProviderConfig(id="tts-1", role="tts", engine="kokoro", voice="bm_daniel")

        assert isinstance(await _make_kokoro_tts(cfg, None), _Tts)
        assert not any(name.startswith("voices/") for name in checked)
        assert constructed[0]["voice"] == "bm_daniel"

    async def test_kokoro_ready_path_loads_the_weights_off_the_event_loop(self, monkeypatch):
        import sys
        import threading
        import types

        built: list[int] = []

        class _KPipeline:
            def __init__(self, lang_code, model=None):
                built.append(threading.get_ident())
                self.model = object()

        monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=_KPipeline))
        monkeypatch.setattr(ai_provider_manager, "hf_hub_download", lambda repo, filename, local_files_only: "/x")
        cfg = ProviderConfig(id="tts-1", role="tts", engine="kokoro", voice="af_heart")

        tts = await _make_kokoro_tts(cfg, None)

        assert tts.loaded  # built at prewarm, not on the first greeting
        assert len(built) == 1 and built[0] != threading.get_ident()

    async def test_kokoro_ready_path_reuses_a_not_ready_instance_that_loaded_and_drops_the_entry(self, monkeypatch):
        import sys
        import types

        class _KPipeline:
            def __init__(self, lang_code, model=None):
                self.model = object()

            def __call__(self, text, voice, speed):
                return iter(())

        weights_missing = True

        def fake_download(repo, filename, local_files_only):
            if weights_missing and filename == "kokoro-v1_0.pth":
                raise LocalEntryNotFoundError("not cached")
            return "/x"

        monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=_KPipeline))
        monkeypatch.setattr(ai_provider_manager, "hf_hub_download", fake_download)
        cfg = ProviderConfig(id="tts-1", role="tts", engine="kokoro", voice="af_heart")
        with pytest.raises(ModelNotReadyError) as exc:
            await _make_kokoro_tts(cfg, None)
        await exc.value.provider.synthesize("hello", 24_000)  # loads it
        weights_missing = False

        assert await _make_kokoro_tts(cfg, None) is exc.value.provider
        assert ai_provider_manager._not_ready_kokoro == {}

    async def test_kokoro_ready_path_builds_fresh_when_the_not_ready_instance_never_loaded(self, monkeypatch):
        import sys
        import types

        class _KPipeline:
            def __init__(self, lang_code, model=None):
                self.model = object()

        weights_missing = True

        def fake_download(repo, filename, local_files_only):
            if weights_missing and filename == "kokoro-v1_0.pth":
                raise LocalEntryNotFoundError("not cached")
            return "/x"

        monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=_KPipeline))
        monkeypatch.setattr(ai_provider_manager, "hf_hub_download", fake_download)
        cfg = ProviderConfig(id="tts-1", role="tts", engine="kokoro", voice="af_heart")
        with pytest.raises(ModelNotReadyError) as exc:
            await _make_kokoro_tts(cfg, None)
        weights_missing = False

        tts = await _make_kokoro_tts(cfg, None)

        assert tts is not exc.value.provider and tts.loaded
        assert ai_provider_manager._not_ready_kokoro == {}

    async def test_faster_whisper_missing_model_raises(self, monkeypatch):
        from ..providers.stt.faster_whisper import FasterWhisperSTT

        async def missing(self, **kw):
            raise LocalEntryNotFoundError("not cached")

        monkeypatch.setattr(FasterWhisperSTT, "load", missing)
        started = []
        monkeypatch.setattr(FasterWhisperSTT, "load_when_needed", lambda self: started.append(self))
        cfg = ProviderConfig(id="stt-1", role="stt", engine="faster_whisper", model="small")

        with pytest.raises(ModelNotReadyError) as exc:
            await _make_faster_whisper(cfg, None)
        with pytest.raises(ModelNotReadyError) as again:
            await _make_faster_whisper(cfg, None)

        assert (exc.value.engine, exc.value.model) == ("faster_whisper", "small")
        # The same instance serves the next call (no second download); its load is just re-armed.
        assert again.value.provider is exc.value.provider
        assert started == [exc.value.provider] * 2

    async def test_a_whisper_model_outside_the_registry_is_never_downloaded(self, monkeypatch):
        from ..providers.stt.faster_whisper import FasterWhisperSTT

        async def missing(self, **kw):
            raise LocalEntryNotFoundError("not cached")

        monkeypatch.setattr(FasterWhisperSTT, "load", missing)
        started = []
        monkeypatch.setattr(FasterWhisperSTT, "load_when_needed", lambda self: started.append(self))
        cfg = ProviderConfig(id="stt-1", role="stt", engine="faster_whisper", model="someone/huge-model")

        with pytest.raises(LocalEntryNotFoundError):  # local-only load fails as it always did
            await _make_faster_whisper(cfg, None)

        assert started == [] and ai_provider_manager._not_ready_whisper == {}

    async def test_a_not_ready_whisper_is_not_handed_to_a_row_with_another_language(self, monkeypatch):
        from ..providers.stt.faster_whisper import FasterWhisperSTT

        async def missing(self, **kw):
            raise LocalEntryNotFoundError("not cached")

        monkeypatch.setattr(FasterWhisperSTT, "load", missing)
        monkeypatch.setattr(FasterWhisperSTT, "load_when_needed", lambda self: None)

        def cfg(language):
            return ProviderConfig(id=f"stt-{language}", role="stt", engine="faster_whisper", model="small", language=language)

        with pytest.raises(ModelNotReadyError) as en:
            await _make_faster_whisper(cfg("en"), None)
        with pytest.raises(ModelNotReadyError) as hi:
            await _make_faster_whisper(cfg("hi"), None)

        assert hi.value.provider is not en.value.provider
        assert (en.value.provider._language, hi.value.provider._language) == ("en", "hi")

    async def test_a_failed_background_whisper_load_is_restarted_by_the_next_get(self, monkeypatch):
        from ..providers.stt.faster_whisper import FasterWhisperSTT

        async def missing(self, **kw):
            raise LocalEntryNotFoundError("not cached")

        starts = []
        monkeypatch.setattr(FasterWhisperSTT, "load", missing)
        monkeypatch.setattr(FasterWhisperSTT, "load_when_needed", lambda self: starts.append(self))
        cfg = ProviderConfig(id="stt-1", role="stt", engine="faster_whisper", model="small")

        for _ in range(2):
            with pytest.raises(ModelNotReadyError):
                await _make_faster_whisper(cfg, None)

        assert len(starts) == 2 and starts[0] is starts[1]

    async def test_faster_whisper_instance_is_returned_once_its_download_finished(self, monkeypatch):
        from ..providers.stt.faster_whisper import FasterWhisperSTT

        async def missing(self, **kw):
            raise LocalEntryNotFoundError("not cached")

        monkeypatch.setattr(FasterWhisperSTT, "load", missing)
        monkeypatch.setattr(FasterWhisperSTT, "load_when_needed", lambda self: None)
        cfg = ProviderConfig(id="stt-1", role="stt", engine="faster_whisper", model="small")
        with pytest.raises(ModelNotReadyError) as exc:
            await _make_faster_whisper(cfg, None)

        exc.value.provider._model = object()  # the background load finished

        assert await _make_faster_whisper(cfg, None) is exc.value.provider

    async def test_ollama_model_not_pulled_raises(self, monkeypatch):
        from ..providers.llm.ollama import OllamaLLM

        async def not_pulled(self):
            return False

        monkeypatch.setattr(OllamaLLM, "model_pulled", not_pulled)
        cfg = ProviderConfig(id="llm-1", role="llm", engine="ollama", model="qwen3:4b")

        with pytest.raises(ModelNotReadyError) as exc:
            await _make_ollama(cfg, None)

        assert (exc.value.engine, exc.value.model) == ("ollama", "qwen3:4b")

    async def test_ollama_base_url_defaults_to_env(self, monkeypatch):
        monkeypatch.setenv("VOICEAI_LLM_URL", "http://ollama:11434")
        cfg = ProviderConfig(id="llm-1", role="llm", engine="ollama", model="llama3.2")

        llm = await _make_ollama(cfg, None)

        assert llm._base_url == "http://ollama:11434"


# ── Cloud engine registry — construction only, no network ─────────────────────

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

    # All are OpenAILLM, so assert the endpoint, not the class name.
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


class TestKokoroDownloadShutdown:
    async def test_shutdown_kills_an_in_flight_kokoro_download_without_waiting_for_it(self, monkeypatch):
        import sys
        import types

        monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=object))
        from ..providers.tts import kokoro as kokoro_module

        started = asyncio.Event()
        spawned = []

        class _Proc:
            returncode = None
            exited = asyncio.Event()

            async def wait(self):
                started.set()
                await self.exited.wait()  # a download that only ends when killed
                return self.returncode

            def kill(self):
                self.returncode = -9
                self.exited.set()

        async def spawn(*argv):
            spawned.append(argv)
            return _Proc()

        def not_cached(*a, **kw):
            raise LocalEntryNotFoundError("not cached")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(kokoro_module, "hf_hub_download", not_cached)
        tts = kokoro_module.KokoroTTS(voice="af_sarah", defer_load=True)
        ai_provider_manager._not_ready_kokoro[("af_sarah", 1.0, "a")] = tts
        synth = asyncio.create_task(tts.synthesize("hello", 16000))
        await started.wait()

        await asyncio.wait_for(close_not_ready_providers(), timeout=2)

        with pytest.raises(RuntimeError):
            await synth
        assert len(spawned) == 1 and spawned[0][-2:] == ("config.json", "kokoro-v1_0.pth")

    async def test_load_with_weights_on_disk_never_fetches_the_voice(self, monkeypatch):
        # Call setup must not wait on (or fail with) a voice download: the voice is fetched on synthesis.
        import sys
        import types

        monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=lambda *a, **kw: object()))
        from ..providers.tts import kokoro as kokoro_module

        fetched = []

        async def fetch(key, *args):
            fetched.append(key)

        def cached_except_voice(repo, filename, local_files_only):
            if filename.startswith("voices/"):
                raise LocalEntryNotFoundError("not cached")
            return "/cache/" + filename

        monkeypatch.setattr(kokoro_module, "hf_hub_download", cached_except_voice)
        monkeypatch.setattr(kokoro_module.model_downloads, "fetch", fetch)
        tts = kokoro_module.KokoroTTS(voice="af_sarah", defer_load=True)

        await tts.load()

        assert fetched == [] and tts.loaded

    async def test_synthesis_never_waits_on_a_shared_download_slot_for_a_voice(self, monkeypatch):
        # Both slots held by multi-GB downloads: a ~0.5 MB voice is left to KPipeline's lazy fetch, so
        # synthesis with the base weights on disk never queues behind them (PR #103 review).
        import sys
        import types

        class _Pipeline:
            def __init__(self, *a, **kw):
                pass

            def __call__(self, text, voice, speed):
                return iter(())

        monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=_Pipeline))
        from ..providers.tts import kokoro as kokoro_module

        def cached_except_voice(repo, filename, local_files_only):
            if filename.startswith("voices/"):
                raise LocalEntryNotFoundError("not cached")
            return "/cache/" + filename

        monkeypatch.setattr(kokoro_module, "hf_hub_download", cached_except_voice)
        slots = kokoro_module.model_downloads._slots
        held = [await slots.acquire() for _ in range(kokoro_module.model_downloads.MAX_CONCURRENT_DOWNLOADS)]
        try:
            tts = kokoro_module.KokoroTTS(voice="af_sarah", defer_load=True)
            await tts.load()
            await asyncio.wait_for(tts.synthesize("hello", 24_000), timeout=2)
        finally:
            for _ in held:
                slots.release()

    async def test_only_the_fixed_base_weights_are_ever_fetched(self, monkeypatch):
        import sys
        import types

        monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=object))
        from ..providers.tts import kokoro as kokoro_module

        spawned = []

        class _Proc:
            returncode = 0

            async def wait(self):
                return 0

        async def spawn(*argv):
            spawned.append(argv)
            return _Proc()

        def not_cached(*a, **kw):
            raise LocalEntryNotFoundError("not cached")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(kokoro_module, "hf_hub_download", not_cached)
        await kokoro_module.KokoroTTS(voice="../../evil", defer_load=True)._fetch_base_weights()

        assert len(spawned) == 1 and spawned[0][-2:] == ("config.json", "kokoro-v1_0.pth")
