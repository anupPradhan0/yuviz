"""Settings that shape voice latency: filler delays, the phrase cache bound, Deepgram endpointing."""

from __future__ import annotations

import importlib

import pytest

from .. import ai_provider_manager as apm
from ..pipeline_config import env_count, env_ms


# ── env parsing: .env.example documents these as blank, so blank must mean "default" ─────────────

@pytest.mark.parametrize("raw,expected", [(None, 400.0), ("", 400.0), ("   ", 400.0), ("250", 250.0),
                                          ("0", 0.0), ("12.5", 12.5), ("abc", 400.0), ("-5", 400.0)])
def test_env_ms_falls_back_to_the_default_for_blank_garbage_or_negative(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("VOICEAI_TEST_MS", raising=False)
    else:
        monkeypatch.setenv("VOICEAI_TEST_MS", raw)

    assert env_ms("VOICEAI_TEST_MS", 400) == expected


@pytest.mark.parametrize("raw,expected", [(None, 64), ("", 64), ("8", 8), ("0", 1), ("-3", 1), ("x", 64)])
def test_env_count_is_at_least_one_and_defaults_on_blank_or_garbage(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("VOICEAI_TEST_COUNT", raising=False)
    else:
        monkeypatch.setenv("VOICEAI_TEST_COUNT", raw)

    assert env_count("VOICEAI_TEST_COUNT", 64) == expected


@pytest.mark.parametrize("module", ["services.conversation.pipeline", "services.conversation.tools.orchestrator",
                                    "services.conversation.phrase_audio"])
def test_a_blank_or_malformed_setting_never_stops_the_service_importing(monkeypatch, module):
    for name in ("VOICEAI_FILLER_DELAY_MS", "VOICEAI_TOOL_FILLER_DELAY_MS", "VOICEAI_PHRASE_CACHE_ENTRIES"):
        monkeypatch.setenv(name, "")
    importlib.reload(importlib.import_module(module))
    for name in ("VOICEAI_FILLER_DELAY_MS", "VOICEAI_TOOL_FILLER_DELAY_MS", "VOICEAI_PHRASE_CACHE_ENTRIES"):
        monkeypatch.setenv(name, "not-a-number")
    importlib.reload(importlib.import_module(module))


def test_the_defaults_are_a_400_ms_delay_for_both_fillers(monkeypatch):
    for name in ("VOICEAI_FILLER_DELAY_MS", "VOICEAI_TOOL_FILLER_DELAY_MS"):
        monkeypatch.delenv(name, raising=False)
    from .. import pipeline
    from ..tools import orchestrator
    importlib.reload(orchestrator)
    importlib.reload(pipeline)

    assert pipeline._FILLER_DELAY_S == pytest.approx(0.4)
    assert orchestrator.SLOW_TOOL_FILLER_AFTER_S == pytest.approx(0.4)


def test_the_delays_can_be_set_from_the_environment(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "300")
    monkeypatch.setenv("VOICEAI_TOOL_FILLER_DELAY_MS", "500")
    from .. import pipeline
    from ..tools import orchestrator
    importlib.reload(orchestrator)
    importlib.reload(pipeline)

    assert pipeline._FILLER_DELAY_S == pytest.approx(0.3)
    assert orchestrator.SLOW_TOOL_FILLER_AFTER_S == pytest.approx(0.5)
    monkeypatch.delenv("VOICEAI_FILLER_DELAY_MS")
    monkeypatch.delenv("VOICEAI_TOOL_FILLER_DELAY_MS")
    importlib.reload(orchestrator)
    importlib.reload(pipeline)


# ── Deepgram endpointing from the provider config ────────────────────────────────────────────────

def _cfg(extra):
    return apm.ProviderConfig(id="stt1", role="stt", engine="deepgram", model="nova-3", language="en",
                              extra=extra, api_key_ref=None)


async def test_deepgram_endpointing_is_off_unless_the_provider_config_sets_it():
    stt = await apm._make_deepgram(_cfg({}), "key")
    assert stt._endpointing_ms is None


async def test_deepgram_endpointing_comes_from_extra():
    stt = await apm._make_deepgram(_cfg({"endpointing_ms": 350}), "key")
    assert stt._endpointing_ms == 350


@pytest.mark.parametrize("bad", ["350", True, 5, 99999, -1, [1]])
async def test_an_unusable_endpointing_value_is_rejected_with_a_clear_message(bad):
    with pytest.raises(ValueError, match="extra.endpointing_ms"):
        await apm._make_deepgram(_cfg({"endpointing_ms": bad}), "key")


def test_env_helpers_share_one_parser_but_keep_their_own_failure_rules(monkeypatch):
    from ..pipeline_config import _env_float, _env_int, env_count, env_ms

    monkeypatch.setenv("X_NUM", "  ")
    assert _env_float("X_NUM", 1.5) == 1.5 and _env_int("X_NUM", 3) == 3 and env_count("X_NUM", 7) == 7
    monkeypatch.setenv("X_NUM", "oops")
    with pytest.raises(ValueError):
        _env_int("X_NUM", 3)                  # established settings still fail loudly on a typo
    assert env_ms("X_NUM", 400) == 400.0 and env_count("X_NUM", 7) == 7
    monkeypatch.setenv("X_NUM", "-5")
    assert env_ms("X_NUM", 400) == 400.0 and env_count("X_NUM", 7) == 1
