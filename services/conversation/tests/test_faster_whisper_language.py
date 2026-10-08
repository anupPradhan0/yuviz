"""FasterWhisperSTT language detection, with a stub model (no weights loaded)."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest


@pytest.fixture
def whisper_cls(monkeypatch):
    # The real package is optional in test envs; the constructor only imports the name.
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=object))
    from services.conversation.providers.stt.faster_whisper import FasterWhisperSTT
    return FasterWhisperSTT


class _StubModel:
    def __init__(self, language="hi", probability=0.93):
        self.calls: list[dict] = []
        self._info = SimpleNamespace(language=language, language_probability=probability)

    def transcribe(self, pcm, **kwargs):
        self.calls.append(kwargs)
        seg = SimpleNamespace(text=" haan ji ", no_speech_prob=0.1, avg_logprob=-0.2)
        return [seg], self._info


def _stt(cls, language, model_size="small", model=None):
    stt = cls(model_size=model_size, language=language)
    stt._model = model or _StubModel()
    return stt


async def test_auto_detect_reports_language_and_probability(whisper_cls):
    stt = _stt(whisper_cls, language=None)
    result = await stt.transcribe(b"\x00\x01" * 8000, 16000)
    assert result.text == "haan ji"
    assert result.language == "hi"
    assert result.language_confidence == pytest.approx(0.93)
    assert stt._model.calls[0]["language"] is None


async def test_forced_language_reports_no_detection(whisper_cls):
    stt = _stt(whisper_cls, language="en")
    result = await stt.transcribe(b"\x00\x01" * 8000, 16000)
    assert result.language is None
    assert stt._model.calls[0]["language"] == "en"


async def test_per_call_language_overrides_instance(whisper_cls):
    stt = _stt(whisper_cls, language="en")
    result = await stt.finalize_stream("s1", b"\x00\x01" * 8000, 16000, language=None)
    assert stt._model.calls[0]["language"] is None
    assert result.language == "hi"


async def test_english_only_model_logs_once_when_asked_to_detect(whisper_cls, caplog):
    stt = _stt(whisper_cls, language=None, model_size="small.en")
    await stt.transcribe(b"\x00\x01" * 8000, 16000)
    await stt.transcribe(b"\x00\x01" * 8000, 16000)
    assert sum("English-only" in r.message for r in caplog.records) == 1
