"""Language registry shared by the Config Service (validation) and the Conversation
Service (runtime). Adding a language is one LANGUAGES row; its spoken system
strings fall back to English until services/conversation/i18n gets a table for it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import Agent, ProviderConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Language:
    code:           str           # ISO 639-1
    name:           str           # English name, used in the LLM instruction
    native_name:    str
    kokoro_code:    str | None    # KPipeline lang_code; None = no Kokoro voice
    deepgram_multi: bool          # covered by Deepgram nova-2/3 language=multi
    # Extra LLM guidance on how to write this language so TTS reads it well.
    script_hint:    str | None = None


LANGUAGES: dict[str, Language] = {l.code: l for l in (
    Language("en", "English",    "English",   "a", True),
    Language("hi", "Hindi",      "हिन्दी",     "h", True,
             "Write Hindi words in Devanagari script and English words in Latin script."),
    Language("es", "Spanish",    "Español",   "e", True),
    Language("fr", "French",     "Français",  "f", True),
    Language("de", "German",     "Deutsch",   None, True),
    Language("pt", "Portuguese", "Português", "p", True),
    Language("it", "Italian",    "Italiano",  "i", True),
    Language("ja", "Japanese",   "日本語",     "j", True),
    Language("zh", "Chinese",    "中文",       "z", False),
)}

# Deepgram's code-switching mode; a valid provider_configs.language for Deepgram STT only.
DEEPGRAM_MULTI = "multi"

# Deepgram nova models that support language=multi.
_DEEPGRAM_MULTI_MODELS = ("nova-2", "nova-3")

_LANG_TAG_RE = re.compile(r"^([A-Za-z]{2,3})(?:[-_].*)?$")


def normalize_language(code: str | None) -> str | None:
    """'hi-IN' / 'hi-Latn' / 'HI' -> 'hi'; None or unparseable -> None."""
    if not code:
        return None
    m = _LANG_TAG_RE.match(code.strip())
    return m.group(1).lower() if m else None


def is_known_language(code: str | None) -> bool:
    return code is not None and code in LANGUAGES


def language_name(code: str) -> str:
    lang = LANGUAGES.get(code)
    return lang.name if lang else code


def deepgram_supports_multi(model: str | None, languages: list[str]) -> bool:
    """True when language=multi on this model can recognise every given language."""
    m = (model or "nova-3").lower()
    if not m.startswith(_DEEPGRAM_MULTI_MODELS):
        return False
    return all(LANGUAGES.get(code) is not None and LANGUAGES[code].deepgram_multi for code in languages)


# ── TTS engine language capability ──────────────────────────────────────────

# Documented per-model language coverage, restricted to registry languages.
_CARTESIA_MULTILINGUAL = frozenset({"en", "hi", "es", "fr", "de", "pt", "it", "ja", "zh"})
_ELEVENLABS_MULTILINGUAL = frozenset({"en", "hi", "es", "fr", "de", "pt", "it", "ja", "zh"})
# ElevenLabs models that speak English only.
_ELEVENLABS_ENGLISH_ONLY_MODELS = ("eleven_monolingual_v1", "eleven_turbo_v2", "eleven_flash_v2")
_KOKORO_LANGUAGES = frozenset(code for code, l in LANGUAGES.items() if l.kokoro_code)


def tts_languages(engine: str, model: str | None) -> frozenset[str]:
    """Registry languages a TTS engine/model can speak. Unknown engines: English only,
    so a new engine must be added here before a multilingual agent can use it."""
    engine = (engine or "").lower()
    if engine == "cartesia":
        m = (model or "sonic-2").lower()
        return frozenset({"en"}) if m.startswith("sonic-english") else _CARTESIA_MULTILINGUAL
    if engine == "elevenlabs":
        m = (model or "eleven_turbo_v2_5").lower()
        return frozenset({"en"}) if m in _ELEVENLABS_ENGLISH_ONLY_MODELS else _ELEVENLABS_MULTILINGUAL
    if engine == "kokoro":
        return _KOKORO_LANGUAGES
    return frozenset({"en"})


def tts_model_of(engine: str, model: str | None, extra: dict | None) -> str | None:
    """The model id the TTS factory actually uses for this row."""
    extra = extra or {}
    if engine == "cartesia":
        return extra.get("model") or model
    if engine == "elevenlabs":
        return extra.get("model_id") or model
    return model


# ── Runtime resolution (shared by every IConfigProvider) ─────────────────────

@dataclass(frozen=True)
class ResolvedLanguages:
    stt_language:        str | None
    tts_language:        str | None
    default_language:    str | None
    supported_languages: tuple[str, ...]


def agent_supported_languages(agent_language: str | None, supported: Any) -> tuple[str, ...]:
    """Normalised, de-duplicated supported list with the default language first.
    Empty = single-language agent."""
    if not supported:
        return ()
    out: list[str] = []
    default = normalize_language(agent_language)
    for code in ([default] if default else []) + [normalize_language(c) for c in supported]:
        if code and code not in out:
            out.append(code)
    return tuple(out)


def resolve_languages(agent: "Agent", stt: "ProviderConfig", tts: "ProviderConfig") -> ResolvedLanguages:
    """Effective per-role languages. Single-language agents: agent.language > the row's own
    language, per role (TTS never inherits the STT row's language). Multilingual agents:
    STT listens for every supported language (Deepgram multi / Whisper auto-detect)."""
    supported = agent_supported_languages(agent.language, agent.supported_languages)
    tts_language = agent.language or tts.language
    if not supported:
        return ResolvedLanguages(agent.language or stt.language, tts_language, None, ())

    default = supported[0]
    if len(supported) == 1:
        stt_language: str | None = default
    elif stt.engine == "deepgram":
        if deepgram_supports_multi(stt.model, list(supported)):
            stt_language = DEEPGRAM_MULTI
        else:
            log.warning(
                "agent=%s: Deepgram model %r cannot code-switch across %s — listening in %s only",
                agent.slug, stt.model, ",".join(supported), default,
            )
            stt_language = default
    elif stt.engine == "faster_whisper":
        stt_language = None  # auto-detect per utterance
    else:
        stt_language = default
    return ResolvedLanguages(stt_language, tts_language or default, default, supported)


def same_tenant_tts_overrides(
    agent: "Agent", supported: tuple[str, ...], configs: dict[str, "ProviderConfig | None"],
) -> dict[str, "ProviderConfig"]:
    """Keep only overrides for supported languages whose row is a TTS config owned by the
    agent's tenant. Provider rows are fetched by id with no tenant scope, so this is the
    runtime half of the check the Config Service makes on write."""
    out: dict[str, ProviderConfig] = {}
    for lang, cfg in configs.items():
        if lang not in supported:
            continue
        if cfg is None:
            log.warning("agent=%s: TTS override for %s not found — using the base voice", agent.slug, lang)
            continue
        if cfg.role != "tts" or cfg.tenant_id is None or str(cfg.tenant_id) != str(agent.tenant_id):
            log.error(
                "agent=%s: TTS override for %s (provider_config %s) rejected — "
                "role=%s tenant=%s, expected a tts config of tenant %s",
                agent.slug, lang, cfg.id, cfg.role, cfg.tenant_id, agent.tenant_id,
            )
            continue
        out[lang] = cfg
    return out
