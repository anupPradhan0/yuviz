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


class _DetectingModel(_StubModel):
    """Whisper hearing Hindi as Urdu: ur tops the probabilities, hi is close behind."""

    def detect_language(self, pcm):
        probs = [("ur", 0.55), ("hi", 0.35), ("en", 0.05), ("es", 0.05)]
        return probs[0][0], probs[0][1], probs


async def test_candidates_fold_urdu_into_hindi_and_decode_in_hindi(whisper_cls):
    stt = _stt(whisper_cls, language=None, model=_DetectingModel())
    result = await stt.finalize_stream("s1", b"\x00\x01" * 8000, 16000, language=None, languages=("en", "hi"))
    assert stt._model.calls[0]["language"] == "hi"  # one decode, in Hindi (Devanagari), not Urdu
    assert result.language == "hi"
    # (ur 0.55 + hi 0.35) / (that + en 0.05): share among the agent's languages.
    assert result.language_confidence == pytest.approx(0.90 / 0.95)


async def test_candidates_pick_best_supported_language(whisper_cls):
    stt = _stt(whisper_cls, language=None, model=_DetectingModel())
    result = await stt.transcribe(b"\x00\x01" * 8000, 16000, language=None, languages=("en", "es"))
    # ur folds into nothing here (hi unsupported): en 0.05 vs es 0.05 — a tie, never confident.
    assert result.language in ("en", "es") and result.language_confidence == pytest.approx(0.5)


async def test_fixed_language_ignores_candidates(whisper_cls):
    stt = _stt(whisper_cls, language="en", model=_DetectingModel())
    result = await stt.transcribe(b"\x00\x01" * 8000, 16000, languages=("en", "hi"))
    assert stt._model.calls[0]["language"] == "en" and result.language is None


class _AccentedEnglishModel(_StubModel):
    """Accented English: Whisper's top pick is English, but most mass is spread elsewhere."""

    def detect_language(self, pcm):
        probs = [("en", 0.48), ("cy", 0.2), ("nl", 0.15), ("hi", 0.06), ("ur", 0.04), ("de", 0.07)]
        return probs[0][0], probs[0][1], probs


async def test_accented_english_is_confidently_english_among_candidates(whisper_cls):
    stt = _stt(whisper_cls, language=None, model=_AccentedEnglishModel())
    result = await stt.transcribe(b"\x00\x01" * 8000, 16000, language=None, languages=("en", "hi"))
    assert result.language == "en"
    assert result.language_confidence == pytest.approx(0.48 / 0.58)  # 0.83: enough to switch back
