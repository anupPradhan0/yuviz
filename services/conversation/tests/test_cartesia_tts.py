"""CartesiaTTS request body: speed is opt-in and shaped per model generation."""

from __future__ import annotations

import pytest

from services.conversation.ai_provider_manager import ProviderConfig, _make_cartesia_tts
from services.conversation.providers.tts.cartesia import CartesiaTTS

_BASE = {
    "model_id": "sonic-2",
    "transcript": "hi",
    "voice": {"mode": "id", "id": "v"},
    "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000},
}


def _body(**kw) -> dict:
    return CartesiaTTS(api_key="k", voice="v", **kw)._body("hi", 16000)


def test_no_speed_leaves_body_unchanged():
    assert _body() == _BASE


def test_sonic3_uses_generation_config():
    assert _body(model="sonic-3", speed=0.85)["generation_config"] == {"speed": 0.85}
    assert "__experimental_controls" not in _body(model="sonic-3", speed=0.85)


def test_sonic2_uses_experimental_controls():
    body = _body(speed=0.75)
    assert body["__experimental_controls"] == {"speed": -0.5}
    assert "generation_config" not in body


def test_speed_is_clamped():
    assert _body(model="sonic-3", speed=9)["generation_config"]["speed"] == 1.5
    assert _body(model="sonic-3", speed=0.1)["generation_config"]["speed"] == 0.6
    assert _body(speed=0.1)["__experimental_controls"]["speed"] == pytest.approx(-0.8)


def _cfg(extra: dict) -> ProviderConfig:
    return ProviderConfig(id="c", role="tts", engine="cartesia", voice="v", extra=extra)


async def test_factory_passes_extra_speed():
    tts = await _make_cartesia_tts(_cfg({"model": "sonic-3", "speed": "0.85"}), "key")
    assert tts._body("hi", 16000)["generation_config"] == {"speed": 0.85}


@pytest.mark.parametrize("bad", ["fast", True, float("nan")])
async def test_factory_rejects_non_numeric_speed(bad):
    with pytest.raises(ValueError, match="extra.speed"):
        await _make_cartesia_tts(_cfg({"speed": bad}), "key")
