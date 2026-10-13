# Code review
VERDICT: AMBER

1. [minor] Kokoro voices outside LOCAL_MODELS can no longer ever become ready — `services/conversation/ai_provider_manager.py:151` — fails when: an existing Kokoro TTS row uses a voice not in the registry (e.g. `af_heart`, which the design and tests themselves use) on a host whose `hf-cache` lacks it: `_make_kokoro_tts` now raises ModelNotReadyError on every call (KPipeline's lazy fetch used to fix this), and `POST /local-models/downloads` returns 400 for that voice (`libs/config_sdk/local_models.py:47`), so the agent falls back to legacy config with no way to fix it from the console — fix: let `trigger` accept any `kokoro` voice id that matches the voice-file pattern, or have the provider-config write reject Kokoro voices that are not in the registry.
