from __future__ import annotations

from libs.config_sdk.providers.mock_provider import MockConfigProvider


async def test_mock_provider_assembles_runtime_config():
    mock = MockConfigProvider()
    mock.add_tenant(
        slug="acme", name="Acme",
        default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
    )
    mock.add_agent("acme", slug="sup", name="Support")
    mock.add_provider_config(id="stt1", role="stt", engine="deepgram")
    mock.add_provider_config(id="llm1", role="llm", engine="openai")
    mock.add_provider_config(id="tts1", role="tts", engine="elevenlabs")

    rc = await mock.get_runtime_config("acme", "sup")
    assert rc is not None
    assert rc.providers.stt.engine == "deepgram"


async def test_mock_provider_unknown_agent_returns_none():
    mock = MockConfigProvider()
    assert await mock.get_runtime_config("acme", "no-such-agent") is None


async def test_mock_provider_get_tools_defaults_empty():
    mock = MockConfigProvider()
    assert await mock.get_tools("acme", "sup") == []
