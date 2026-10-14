"""
resolve_handler_deps(): all-or-nothing fallback contract, with a mock config provider
and fake provider registry (no I/O).
"""

from __future__ import annotations

import dataclasses
import logging

from libs.config_sdk.providers.mock_provider import MockConfigProvider

from ..agent_resolver import resolve_handler_deps
from ..ai_provider_manager import AIProviderManager, ModelNotReadyError, ProviderConfig
from ..provider_bundle import ProviderBundle, ProviderRegistry


class FakeProviderInstance:
    def __init__(self, cfg: ProviderConfig, api_key: str | None) -> None:
        self.cfg = cfg
        self.api_key = api_key


async def _fake_factory(cfg: ProviderConfig, api_key: str | None) -> FakeProviderInstance:
    return FakeProviderInstance(cfg, api_key)


class FakeSecretResolver:
    async def resolve(self, ref: str) -> str:
        return f"resolved:{ref}"


FAKE_REGISTRY = {
    ("stt", "fake_stt"): _fake_factory,
    ("llm", "fake_llm"): _fake_factory,
    ("tts", "fake_tts"): _fake_factory,
}


def _registry() -> ProviderRegistry:
    manager = AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY)
    return ProviderRegistry(manager)


def _fully_configured_mock() -> MockConfigProvider:
    mock = MockConfigProvider()
    mock.add_tenant(
        slug="acme", name="Acme",
        default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
    )
    mock.add_agent(
        "acme", slug="sup", name="Sup", greeting="Hi there", system_prompt="Be helpful.",
    )
    mock.add_provider_config(id="stt1", role="stt", engine="fake_stt")
    mock.add_provider_config(id="llm1", role="llm", engine="fake_llm")
    mock.add_provider_config(id="tts1", role="tts", engine="fake_tts")
    return mock


async def test_no_agent_row_returns_none():
    mock = MockConfigProvider()
    mock.add_tenant(slug="acme", name="Acme")

    result = await resolve_handler_deps("acme", "no-such-agent", _registry(), mock)
    assert result is None


async def test_no_tenant_returns_none():
    mock = MockConfigProvider()
    result = await resolve_handler_deps("no-such-tenant", "no-such-agent", _registry(), mock)
    assert result is None


async def test_full_resolution_returns_runtime_config_and_provider_bundle():
    mock = _fully_configured_mock()

    result = await resolve_handler_deps("acme", "sup", _registry(), mock)

    assert result is not None
    runtime_config, bundle = result
    assert isinstance(bundle.stt, FakeProviderInstance) and bundle.stt.cfg.engine == "fake_stt"
    # bundle.llm is RetryOnceLLM-wrapped.
    assert isinstance(bundle.llm._llm, FakeProviderInstance) and bundle.llm._llm.cfg.engine == "fake_llm"
    assert isinstance(bundle.tts, FakeProviderInstance) and bundle.tts.cfg.engine == "fake_tts"
    assert runtime_config.conversation.greeting == "Hi there"
    assert runtime_config.conversation.system_prompt == "Be helpful."
    assert runtime_config.policies.goodbye_grace_ms == 3000  # schema default
    assert runtime_config.version == 1


async def test_extra_jsonb_dict_passes_through_to_provider_config():
    mock = _fully_configured_mock()
    mock.provider_configs["stt1"] = dataclasses.replace(mock.provider_configs["stt1"], extra={"device": "cpu"})

    result = await resolve_handler_deps("acme", "sup", _registry(), mock)

    assert result is not None
    _, bundle = result
    assert bundle.stt.cfg.extra == {"device": "cpu"}


async def test_missing_provider_config_role_returns_none():
    # No tts default: incomplete config is unavailable, not partially applied.
    mock = MockConfigProvider()
    mock.add_tenant(slug="acme", name="Acme", default_stt_config_id="stt1", default_llm_config_id="llm1")
    mock.add_agent("acme", slug="sup", name="Sup")
    mock.add_provider_config(id="stt1", role="stt", engine="fake_stt")
    mock.add_provider_config(id="llm1", role="llm", engine="fake_llm")

    result = await resolve_handler_deps("acme", "sup", _registry(), mock)
    assert result is None


async def test_inactive_agent_returns_none_falls_back_to_legacy():
    mock = _fully_configured_mock()
    mock.agents[("acme", "sup")] = dataclasses.replace(mock.agents[("acme", "sup")], status="inactive")

    result = await resolve_handler_deps("acme", "sup", _registry(), mock)
    assert result is None


async def test_model_not_ready_returns_none_with_one_warning_naming_the_model(caplog):
    async def not_ready_factory(cfg: ProviderConfig, api_key: str | None):
        raise ModelNotReadyError("fake_tts", "af_heart")

    manager = AIProviderManager(
        FakeSecretResolver(), registry={**FAKE_REGISTRY, ("tts", "fake_tts"): not_ready_factory}
    )

    with caplog.at_level(logging.WARNING, logger="services.conversation.agent_resolver"):
        result = await resolve_handler_deps(
            "acme", "sup", ProviderRegistry(manager), _fully_configured_mock()
        )

    assert result is None
    assert len(caplog.records) == 1
    assert "af_heart" in caplog.records[0].getMessage()
    assert "acme" not in caplog.records[0].getMessage()


async def test_a_routed_agent_whose_tts_is_not_ready_still_gets_a_bundle():
    async def not_ready_factory(cfg: ProviderConfig, api_key: str | None):
        raise ModelNotReadyError("fake_tts", "af_heart", provider="not-ready-tts")

    manager = AIProviderManager(
        FakeSecretResolver(), registry={**FAKE_REGISTRY, ("tts", "fake_tts"): not_ready_factory}
    )
    registry = ProviderRegistry(manager)
    registry.set_defaults(
        ProviderBundle(stt="default-stt", llm="default-llm", tts="default-tts"),
        stt_languages=frozenset({"en"}), tts_languages=frozenset({"en"}),
    )

    result = await resolve_handler_deps("acme", "sup", registry, _fully_configured_mock())

    assert result is not None
    _, bundle = result
    assert bundle.tts == "default-tts"  # the agent is English; only the TTS role is swapped
    assert isinstance(bundle.stt, FakeProviderInstance)
