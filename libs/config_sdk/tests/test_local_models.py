"""Local model registry: lookup and agreement with the language capability functions."""

from __future__ import annotations

from libs.config_sdk.languages import stt_languages, tts_languages
from libs.config_sdk.local_models import LOCAL_MODELS, find_local_model


def test_find_local_model_returns_entry():
    m = find_local_model("faster_whisper", "small")
    assert m is not None and m.role == "stt"


def test_find_local_model_unknown_pair_is_none():
    assert find_local_model("faster_whisper", "no-such-size") is None
    assert find_local_model("deepgram", "small") is None


def test_llm_entries_have_no_languages():
    llms = [m for m in LOCAL_MODELS if m.role == "llm"]
    assert llms and all(m.languages is None for m in llms)


def test_non_llm_languages_match_capability_functions():
    for m in LOCAL_MODELS:
        if m.role == "stt":
            assert m.languages == stt_languages(m.engine, m.name)
        elif m.role == "tts":
            assert m.languages == tts_languages(m.engine, None, m.name)


def test_kokoro_entries_only_advertise_supported_languages():
    kokoro = [m for m in LOCAL_MODELS if m.engine == "kokoro"]
    assert kokoro
    for m in kokoro:
        # Voices of a language the registry cannot run (e.g. Japanese) advertise none: downloadable, never offered.
        assert m.languages <= tts_languages("kokoro", None, m.name)


def test_english_only_whisper_checkpoints():
    assert find_local_model("faster_whisper", "small.en").languages == frozenset({"en"})
    assert "hi" in find_local_model("faster_whisper", "small").languages
