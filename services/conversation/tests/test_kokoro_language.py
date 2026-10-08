"""KokoroTTS per-language pipelines, with a stub `kokoro` module (no weights)."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest


class _StubPipeline:
    built: list[tuple[str, object]] = []

    def __init__(self, lang_code, model=True):
        self.lang_code = lang_code
        self.model = model if model is not True else object()
        _StubPipeline.built.append((lang_code, model))

    def __call__(self, text, voice, speed):
        yield text, None, np.zeros(240, dtype=np.float32)


@pytest.fixture
def kokoro(monkeypatch):
    _StubPipeline.built = []
    monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=_StubPipeline))
    from services.conversation.providers.tts import kokoro as mod
    return mod


def test_iso_to_kokoro_lang_code(kokoro):
    assert kokoro.kokoro_lang_code("en") == "a"
    assert kokoro.kokoro_lang_code("hi") == "h"
    assert kokoro.kokoro_lang_code("hi-IN") == "h"
    expected = {"es": "e", "fr": "f", "it": "i", "pt": "p", "ja": "j", "zh": "z"}
    assert {k: kokoro.kokoro_lang_code(k) for k in expected} == expected
    assert kokoro.kokoro_lang_code("de") is None
    assert kokoro.kokoro_lang_code(None) is None


async def test_pipelines_are_lazy_cached_and_share_the_model(kokoro):
    tts = kokoro.KokoroTTS(voice="af_sarah", lang_code="a")
    assert [code for code, _ in _StubPipeline.built] == ["a"]
    base_model = tts._pipelines["a"].model

    await tts.synthesize("नमस्ते", 16000, language="hi")
    await tts.synthesize("फिर से", 16000, language="hi")
    await tts.synthesize("hello", 16000)

    assert [code for code, _ in _StubPipeline.built] == ["a", "h"]
    assert _StubPipeline.built[1][1] is base_model


async def test_unknown_language_falls_back_to_row_pipeline(kokoro):
    tts = kokoro.KokoroTTS(lang_code="a")
    audio = await tts.synthesize("Hallo", 16000, language="de")
    assert audio
    assert [code for code, _ in _StubPipeline.built] == ["a"]


async def test_prewarm_builds_pipelines_up_front(kokoro):
    tts = kokoro.KokoroTTS(lang_code="a")
    await tts.prewarm(["en", "hi", "es"])
    assert sorted(code for code, _ in _StubPipeline.built) == ["a", "e", "h"]
