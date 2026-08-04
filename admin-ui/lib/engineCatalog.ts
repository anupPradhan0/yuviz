// Static catalog of known engines/models/voices, matching exactly what
// services/conversation/ai_provider_manager.py's _DEFAULT_REGISTRY actually
// implements — every engine listed here is real and instantiable, not
// aspirational (cloud engines deepgram/openai/elevenlabs were added this
// session; see project memory).
//
// Model/voice lists are "known-good starting points," not exhaustive or
// enforced server-side (provider_configs.model/voice are plain TEXT columns)
// — the UI always offers an "Other (custom)" escape hatch per field, since
// e.g. Ollama's actual available models depend on what's pulled locally, and
// ElevenLabs voice_ids are account-specific (never guessed/hardcoded here).

import { ProviderRole } from "./api";

export interface EngineOption {
  value: string;
  label: string;
}

export const ENGINES_BY_ROLE: Record<ProviderRole, EngineOption[]> = {
  stt: [
    { value: "faster_whisper", label: "FasterWhisper (local)" },
    { value: "deepgram", label: "Deepgram (cloud)" },
  ],
  llm: [
    { value: "ollama", label: "Ollama (local)" },
    { value: "openai", label: "OpenAI (cloud)" },
    { value: "gemini", label: "Gemini (cloud)" },
  ],
  tts: [
    { value: "macos", label: "macOS say (local)" },
    { value: "kokoro", label: "Kokoro (local)" },
    { value: "elevenlabs", label: "ElevenLabs (cloud)" },
  ],
  embedding: [
    { value: "ollama", label: "Ollama (local)" },
    { value: "openai", label: "OpenAI (cloud)" },
  ],
};

// null = no fixed list; render a free-text input instead of a dropdown.
export const MODELS_BY_ENGINE: Record<string, string[] | null> = {
  // .en variants skip language auto-detection entirely — the recommended
  // default for English-only agents (see pipeline_config.py's SttConfig
  // docstring: eliminates the class of bug where a short noise blip gets
  // transcribed as a wrong-language hallucination instead of ignored).
  faster_whisper: ["small.en", "tiny.en", "base.en", "medium.en", "tiny", "base", "small", "medium", "large-v3"],
  deepgram: ["nova-3", "nova-2"],
  ollama: ["llama3.2", "llama3", "mistral", "phi3", "gemma2"],
  openai: ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
  // "-latest" aliases stay pointed at Google's current stable model as
  // pinned versions are deprecated for new callers over time (confirmed
  // live 2026-07-22: gemini-2.5-flash itself returned 404 "no longer
  // available to new users" — the alias is the resilient default).
  gemini: ["gemini-flash-latest", "gemini-pro-latest", "gemini-2.5-flash", "gemini-2.5-pro"],
};

// Kept separate from MODELS_BY_ENGINE rather than merged into it: "ollama"
// and "openai" are shared engine names across the llm and embedding roles,
// but their model lists are completely different catalogs (llama3.2 is not
// an embedding model) — a single engine-keyed map can't represent that.
// Engine-level defaults in embedding_manager.py (nomic-embed-text for
// ollama, text-embedding-3-small for openai) apply whenever model is left
// unset — leaving this unset is a perfectly normal choice, not a gap.
export const EMBEDDING_MODELS_BY_ENGINE: Record<string, string[] | null> = {
  ollama: ["nomic-embed-text", "mxbai-embed-large", "all-minilm"],
  openai: ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"],
};

// Gender is metadata for filtering/labeling the picker UI only — it is
// never sent to any TTS engine. No engine takes "gender" as a synthesis
// parameter; it's a fixed property of each concrete voice id (Kokoro
// encodes it in the name prefix, macOS voices have fixed built-in
// genders). "neutral" exists in the type for future engines/voices; no
// currently-shipped local voice qualifies, so none is labeled that way —
// see VoicePicker's gender filter, which only shows chips that have at
// least one matching voice.
export type VoiceGender = "female" | "male" | "neutral";

export interface VoiceOption {
  id:     string;
  label:  string;
  gender: VoiceGender;
}

export const VOICES_BY_ENGINE: Record<string, VoiceOption[] | null> = {
  macos: [
    { id: "Samantha", label: "Samantha", gender: "female" },
    { id: "Karen",    label: "Karen",    gender: "female" },
    { id: "Moira",    label: "Moira",    gender: "female" },
    { id: "Alex",     label: "Alex",     gender: "male" },
    { id: "Daniel",   label: "Daniel",   gender: "male" },
  ],
  // af_/bf_ = American/British female, am_/bm_ = American/British male
  // (Kokoro's own naming convention) — full set of voices shipped with
  // the installed model, not just the original 5.
  kokoro: [
    { id: "af_sarah",  label: "Sarah (US)",   gender: "female" },
    { id: "af_bella",  label: "Bella (US)",   gender: "female" },
    { id: "af_nicole", label: "Nicole (US)",  gender: "female" },
    { id: "bf_emma",   label: "Emma (UK)",    gender: "female" },
    { id: "bf_isabella", label: "Isabella (UK)", gender: "female" },
    { id: "am_adam",   label: "Adam (US)",    gender: "male" },
    { id: "am_michael", label: "Michael (US)", gender: "male" },
    { id: "bm_george", label: "George (UK)",  gender: "male" },
    { id: "bm_lewis",  label: "Lewis (UK)",   gender: "male" },
  ],
  elevenlabs: null, // account-specific voice_id — never guessed, always free text
};

// agents.language — an explicit per-agent override (see lib/api.ts's Agent
// interface); this is a curated starting list for the dropdown, not an
// enforced set (the column is plain TEXT, same posture as engine/model/
// voice above) — "Other (custom)" always escapes to free text for a
// language/locale not listed here.
export const LANGUAGES = [
  { value: "en", label: "English" },
  { value: "en-US", label: "English (US)" },
  { value: "en-GB", label: "English (UK)" },
  { value: "es", label: "Spanish" },
  { value: "fr", label: "French" },
  { value: "de", label: "German" },
  { value: "it", label: "Italian" },
  { value: "pt", label: "Portuguese" },
  { value: "hi", label: "Hindi" },
  { value: "ja", label: "Japanese" },
  { value: "zh", label: "Chinese" },
  { value: "ar", label: "Arabic" },
];

export const OTHER = "__other__";
