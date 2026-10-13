"""Registry of the free local models the Config Service can download and the admin UI can offer.
Only entries here can be pulled, so the registry is also the allow-list for downloads.
Languages come from the capability functions in languages.py, never restated here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .languages import stt_languages, tts_languages


@dataclass(frozen=True)
class LocalModel:
    engine:    str
    role:      str                    # stt | llm | tts
    name:      str                    # model id, or voice id for TTS
    label:     str
    languages: frozenset[str] | None  # None = not language-bound (LLMs)


def _stt(engine: str, name: str) -> LocalModel:
    return LocalModel(engine, "stt", name, name, stt_languages(engine, name))


def _llm(engine: str, name: str) -> LocalModel:
    return LocalModel(engine, "llm", name, name, None)


def _tts(engine: str, name: str, label: str) -> LocalModel:
    return LocalModel(engine, "tts", name, label, tts_languages(engine, None, name))


# Every voice of the official Kokoro-82M v1.0 release; the first letter is the voice's language.
_KOKORO_REGION = {
    "a": "US", "b": "UK", "e": "Spanish", "f": "French", "h": "Hindi",
    "i": "Italian", "j": "Japanese", "p": "Portuguese", "z": "Chinese",
}
_KOKORO_VOICES = (
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova",
    "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    "ef_dora", "em_alex", "em_santa",
    "ff_siwis",
    "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "if_sara", "im_nicola",
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo",
    "pf_dora", "pm_alex", "pm_santa",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi", "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
)

LOCAL_MODELS: tuple[LocalModel, ...] = (
    *(_stt("faster_whisper", n) for n in (
        "tiny", "base", "small", "medium", "large-v3",
        "tiny.en", "base.en", "small.en", "medium.en",
    )),
    *(_llm("ollama", n) for n in (
        "llama3.2", "llama3", "qwen2.5", "mistral", "phi3", "gemma3:4b", "gemma4:e2b",
    )),
    *(_tts("kokoro", n, f"{n[3:].title()} ({_KOKORO_REGION[n[0]]})") for n in _KOKORO_VOICES),
    *(_tts("macos", n, n) for n in ("Samantha", "Karen", "Moira", "Alex", "Daniel")),
)

_BY_KEY = {(m.engine, m.name): m for m in LOCAL_MODELS}


def find_local_model(engine: str, name: str) -> LocalModel | None:
    return _BY_KEY.get((engine, name))
