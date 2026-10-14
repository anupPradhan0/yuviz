from __future__ import annotations

import httpx
import pytest

from services.config import cache, provider_configs


async def test_create_and_get_provider_config(test_tenant, scoped):
    created = await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Deepgram Nova-3", role="stt", engine="deepgram",
        environment="prod", model="nova-3", api_key_ref="k8s:voiceai/deepgram-api-key",
        allow_pointer_schemes=True,
    )
    assert created["role"] == "stt"
    assert created["environment"] == "prod"
    assert created["api_key_ref"] == "k8s:voiceai/deepgram-api-key"

    fetched = await provider_configs.get_provider_config(created["id"])
    assert fetched is not None
    assert fetched["engine"] == "deepgram"


async def test_list_provider_configs_filters_by_role_and_environment(test_tenant, scoped):
    await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Deepgram", role="stt", engine="deepgram", environment="prod",
        allow_pointer_schemes=False,
    )
    await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Whisper", role="stt", engine="faster_whisper", environment="dev",
        allow_pointer_schemes=False,
    )
    await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="GPT-4o", role="llm", engine="openai", environment="prod",
        allow_pointer_schemes=False,
    )

    stt_only = await provider_configs.list_provider_configs(test_tenant["id"], role="stt")
    assert {p["engine"] for p in stt_only} == {"deepgram", "faster_whisper"}

    prod_stt_only = await provider_configs.list_provider_configs(
        test_tenant["id"], role="stt", environment="prod",
    )
    assert [p["engine"] for p in prod_stt_only] == ["deepgram"]


async def test_update_provider_config_invalidates_cache(test_tenant, scoped):
    created = await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Deepgram", role="stt", engine="deepgram",
        allow_pointer_schemes=False,
    )
    await provider_configs.get_provider_config(created["id"])  # warm cache
    assert await cache.get_json(f"provider:{created['id']}") is not None

    updated = await provider_configs.update_provider_config(created["id"], model="nova-3-medical", allow_pointer_schemes=False)
    assert updated["model"] == "nova-3-medical"
    assert await cache.get_json(f"provider:{created['id']}") is None


async def test_update_provider_config_publishes_change_notification(test_tenant, scoped):
    """Conversation Service relies on this channel/message shape to evict cached clients (real Redis)."""
    created = await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Deepgram", role="stt", engine="deepgram",
        allow_pointer_schemes=False,
    )

    subscriber = cache.get_client().pubsub()
    await subscriber.subscribe(provider_configs.PROVIDER_CONFIG_CHANGED_CHANNEL)
    await subscriber.get_message(timeout=1)  # the subscribe confirmation itself

    await provider_configs.update_provider_config(created["id"], model="nova-3-medical", allow_pointer_schemes=False)

    message = await subscriber.get_message(timeout=2)
    assert message is not None
    assert message["type"] == "message"
    assert message["data"] == str(created["id"])

    await subscriber.unsubscribe(provider_configs.PROVIDER_CONFIG_CHANGED_CHANNEL)
    await subscriber.aclose()


async def test_provider_config_role_check_constraint_rejects_bad_role(test_tenant, scoped):
    with pytest.raises(Exception):
        await provider_configs.create_provider_config(
            tenant_id=test_tenant["id"], name="Bad", role="not-a-real-role", engine="x",
            allow_pointer_schemes=False,
        )


async def test_audit_log_redacts_api_key_ref(test_tenant, scoped, pool):
    created = await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Deepgram", role="stt", engine="deepgram",
        api_key_ref="k8s:voiceai/deepgram-api-key",
        allow_pointer_schemes=True,
    )
    row = await pool.fetchrow(
        "SELECT * FROM audit_log WHERE entity_type = 'provider_config' AND entity_id = $1 "
        "ORDER BY changed_at DESC LIMIT 1",
        created["id"],
    )
    assert row is not None
    import json
    new_value = json.loads(row["new_value"])
    assert new_value["api_key_ref"] == "[redacted]"


class _FakeResolver:
    async def resolve(self, ref):
        return "secret-key"


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient; records the request and returns a canned response."""

    calls: list[tuple[str, dict]] = []
    payload: object = None

    def __init__(self, **_):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers):
        type(self).calls.append((url, headers))
        return httpx.Response(200, json=type(self).payload, request=httpx.Request("GET", url))


@pytest.fixture
def fake_http(monkeypatch):
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(provider_configs.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


def _tts_config(monkeypatch, engine):
    cfg = {"id": "p1", "role": "tts", "engine": engine, "api_key_ref": "k8s:voiceai/key"}

    async def fake_get(provider_id):
        return cfg

    monkeypatch.setattr(provider_configs, "get_provider_config", fake_get)
    return cfg


async def test_list_provider_voices_elevenlabs_shape_unchanged(monkeypatch, fake_http):
    fake_http.payload = {"voices": [{
        "voice_id": "v1", "name": "Rachel", "category": "premade", "labels": {"accent": "us"},
        "preview_url": "https://x/p.mp3", "verified_languages": ["en"],
    }]}
    cfg = _tts_config(monkeypatch, "elevenlabs")

    voices = await provider_configs.list_provider_voices(cfg["id"], secret_resolver=_FakeResolver())

    assert voices == [{
        "voice_id": "v1", "name": "Rachel", "category": "premade", "labels": {"accent": "us"},
        "preview_url": "https://x/p.mp3", "verified_languages": ["en"],
    }]
    assert fake_http.calls == [("https://api.elevenlabs.io/v1/voices", {"xi-api-key": "secret-key"})]


async def test_list_provider_voices_cartesia_maps_language(monkeypatch, fake_http):
    fake_http.payload = [
        {"id": "c1", "name": "Katie", "language": "en"},
        {"id": "c2", "name": "Sin idioma"},
    ]
    cfg = _tts_config(monkeypatch, "cartesia")

    voices = await provider_configs.list_provider_voices(cfg["id"], secret_resolver=_FakeResolver())

    # Same object shape as ElevenLabs: the admin UI reads `.language` from each entry.
    assert [(v["voice_id"], v["name"], [x["language"] for x in v["verified_languages"]]) for v in voices] == [
        ("c1", "Katie", ["en"]), ("c2", "Sin idioma", []),
    ]
    assert set(voices[0]["verified_languages"][0]) == {"language", "model_id", "accent", "locale", "preview_url"}
    url, headers = fake_http.calls[0]
    assert url == "https://api.cartesia.ai/voices"
    assert headers["X-API-Key"] == "secret-key"
