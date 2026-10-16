# PRD: Monolingual / multilingual agent setup with language-aware free models and RAG replies

## Problem
Agent creators today pick a default language plus any number of secondary languages (`admin-ui/components/AgentVoiceSettings.tsx`, `admin-ui/app/(console)/agents/new/page.tsx`), where "multilingual" is only the implicit result of having 2+ entries. Model and voice dropdowns do not narrow to what actually speaks the chosen language(s). Free local models that are not yet downloaded fail at first call, not at setup. The knowledge-search path has no language rule of its own, so a bot can answer in the language of the retrieved document.

Market research skipped: this is codebase-specific (our provider catalogue, local model runtime and agent editor), not a solved generic problem. The existing design is in `docs/multilingual-agents.md`.

## Scope
- In:
  - An explicit, stored language mode on the agent: Monolingual (exactly 1 language) or Multilingual (exactly 2 languages). The cap is 2 and a Multilingual agent must have both. The language list remains the source of truth for which languages; the mode says which rule applies. The old default-plus-secondaries picker is replaced.
  - In the native/free pipeline, the free STT dropdown lists only free models that support every selected language, and each language's free TTS voice dropdown lists only voices that support that language. Free LLMs are all shown (per-language LLM filtering is deferred).
  - A free model that is selected but not yet downloaded starts downloading automatically in the backend, only for models in the server-side model registry, with a visible status in the UI (downloading, ready, failed) and a way to retry.
  - For premium TTS providers, the voice list is populated with voices that cover the selected language(s), including multilingual voices. For free TTS in Multilingual mode, one voice is picked per language.
  - A cleaned-up agent creation flow: mode and language(s) first, then models and voices filtered by that choice, with validation messages at the point of error.
  - RAG answer language: in multilingual mode the answer is in the language of the user's question. In monolingual mode it is always in the configured language, whatever language the retrieved documents are in.
- Out:
  - More than 2 languages, per-utterance translation of documents, or a translation service.
  - Per-language filtering of free LLMs (deferred; no per-language LLM data exists in the catalogue).
  - Adding new languages to the registry (`libs/config_sdk/languages.py`) or new vendors.
  - Re-ingesting or re-indexing knowledge bases.
  - Changing the Easy-mode templates beyond what the new language step needs.

Assumption: "use orchestrator pipeline" means this work is run through the SDLC pipeline (`.claude/README.md`), not a runtime orchestrator feature.
Assumption: "free models" means the local/self-hosted engines already in `admin-ui/lib/engineCatalog.ts` (FasterWhisper, Ollama, Kokoro, macOS say). "Premium" means hosted providers (Deepgram, ElevenLabs, Cartesia).
Assumption: existing agents with `supported_languages` of 3 or more are not silently truncated; they keep running and the editor requires them to be reduced to 2 on the next save.
Assumption: a legacy agent (no stored mode) with 0 languages opens as Monolingual with the tenant default language pre-filled, and saving it persists that language; with 1 language it opens as Monolingual; with 2 it opens as Multilingual.
Assumption: the automatic download starts when the model is selected in the form, before the agent is saved (the request asks for download "automatically" on selection). Abandoned forms may therefore leave a downloaded model on the host; that is accepted.

## Acceptance criteria
1. Given the new-agent form, when it opens, then the user must choose Monolingual or Multilingual, and the language picker matches the mode.
2. Given Monolingual mode, when the user picks a language, then exactly one language is selected, and picking another replaces it.
3. Given Multilingual mode, when the user has selected 2 languages, then every other language option is disabled with a "maximum 2 languages" message.
4. Given a save request (UI or API) with a 3rd language in multilingual mode, when it is submitted, then the server rejects it with a 4xx naming the limit, even if the UI check is bypassed.
5. Given a save request with mode Monolingual and 0 or 2+ languages, when submitted, then the server rejects it with a 4xx.
6. Given Multilingual mode with fewer than 2 languages selected (exactly 2 are required), when the user saves, then save is blocked with a message asking for a second language, and the server rejects the same request with a 4xx.
7. Given a save request with a mode value other than monolingual or multilingual, when submitted, then the server rejects it with a 4xx.
8. Given Monolingual mode with language L, when the free STT dropdown opens, then it lists only free STT models that support L, and none that do not (e.g. no `.en` Whisper model for Hindi).
9. Given Multilingual mode with languages A and B, when the free STT dropdown opens, then it lists only STT models that support both A and B; free TTS is filtered per language by criterion 17, not by both languages at once.
10. Given any language selection, when the free LLM dropdown opens, then all free LLMs are listed regardless of language.
11. Given a language selection for which no free STT or TTS model qualifies, when that dropdown opens, then it shows an explicit "no free model supports this language" state and points to premium options, not an empty list.
12. Given a free model that is not downloaded, when the user selects it in the form (before the agent is saved), then the backend starts the download without further user action, and selecting the same model again, or from another form, while a download is running does not start a second download.
13. Given a download trigger request naming an (engine, model) pair that is not in the server-side model registry, when it is submitted, then the server rejects it with a 4xx and starts no download.
14. Given the number of concurrent downloads on a host is at its cap, when another download is triggered, then it is queued or refused with 429, and the running downloads are unaffected.
15. Given a model download in progress, when the user views the agent form, then the UI shows its downloading status; Test is blocked until the model is ready, and Save is allowed with a visible warning.
16. Given a failed download, when the user views the form, then the error is shown and a retry action restarts it.
17. Given Multilingual mode with free TTS, when the user configures voices, then each language's free TTS voice dropdown is filtered by that language alone, they pick one voice per language, each voice supporting its own language, and the choices are stored through the existing `tts_config_by_language` mapping; a single voice covering both languages is not required.
18. Given a model already downloaded, when it is selected, then no download is started and its status reads ready.
19. Given a language change after a free STT or TTS model was selected, when that model no longer supports the new language(s), then it is cleared with a visible notice, not kept silently.
20. Given a selected model that does not support a selected language (for example via an API request or a stale form), when the user saves, then save is blocked with a message naming the model and language, and the server rejects the same request with a 4xx.
21. Given a selected model that does not support a selected language, when the user runs Test, then Test proceeds and shows a non-blocking warning naming the model and language.
22. Given a premium TTS provider and the selected language(s), when the voice list loads, then it contains only voices that can speak the selected language(s); in multilingual mode it may use a single multilingual voice covering both languages.
23. Given a premium provider whose voice list cannot be fetched (bad key, network), when the form loads, then it shows an error and still allows entering a voice ID by hand, as ElevenLabs does today.
24. Given any advertised engine-language pair, when an agent uses it, then the runtime path works in this environment (dependency installed, parameter accepted) or falls back with one logged warning instead of going silent (lessons 45, 46).
25. Given a Multilingual agent with a knowledge base, when the user asks a question in language A, then the reply is in language A, even if the retrieved passages are in language B.
26. Given a Multilingual agent, when the user switches question language mid-call, then the next RAG answer follows the new language.
27. Given a Monolingual agent set to L with retrieved passages in another language, when the user asks in any language, then the reply is in L.
28. Given a Monolingual agent, when the call starts, then no per-utterance language switching occurs.
29. Given a RAG answer via the `search_knowledge` tool path (`services/conversation/pipeline.py`) or the pre-retrieval context path, when either runs, then both obey criteria 25 to 27.
30. Given an agent created before this change (stored mode absent, with `supported_languages` of 0, 1 or 2), when it is opened, then it shows the mode per the assumption above, and it runs unchanged until edited; a 0-language agent saved from the editor persists the tenant default language as its one language. An agent with 3 or more is shown with a prompt to reduce to 2 and still runs.
31. Given any authorized caller of the model-catalogue or download routes (status and trigger), when they request the same model, then the response is identical regardless of the caller's tenant, and it contains no tenant, agent or user data (models are shared per host).
32. Given a caller without edit rights on agents (e.g. viewer), when they try to trigger a model download, then the request is refused.
33. Given the old UI path that built `supported_languages` by toggling secondary languages, when the new editor ships, then no caller (`new/page.tsx`, `[agentSlug]/page.tsx`, `agentDraft.ts`, `api.ts`) still uses it.
34. Given a saved agent whose free model is still downloading, failed to download, or is otherwise absent from the host, when a live call to that agent starts or reaches that model, then the call does not go silent: it falls back or rejects with exactly one logged warning naming the model, and the agent shows a "model not ready" state in the console.

## Constraints
- The server is the authority on the cap, the mode and model-language support; the UI mirrors it. The language registry is `libs/config_sdk/languages.py`, mirrored in `admin-ui/lib/engineCatalog.ts`. Capability data must not be defined in two diverging places (lesson 46). The model registry that gates downloads is the same capability source.
- Values handed to a provider are normalised at the adapter (lesson 45). Existing agent fields (`language`, `supported_languages`, `tts_config_by_language`, `greeting_by_language`) in `services/config/agents.py` and `services/config/schemas.py` must stay readable for old rows; the new mode field must tolerate its absence on old rows.
- Agent <-> provider-config links stay tenant-checked (lesson 48). Download trigger and status routes must follow the Config service auth and tenant conventions (`services/config/deps.py`, lessons 2, 24, 27).
- Downloads must not block the event loop (lesson 18) and must have a lifecycle (lesson 26). Model files are shared per host, not per tenant, so status exposes nothing tenant-specific. The download trigger accepts only registry models and is capped per host (criteria 13, 14).
- Any new schema change goes in `database/schema.sql`, applied by plain `psql -f` (lessons 10, 13). Stored-data changes ship with the reader (lesson 11).
- All callers of changed API fields are updated across `admin-ui/`, `scripts/` and `docs/` (lesson 17). Easy-mode templates are versioned; read version-independent properties by id (lesson 47).
- Multilingual docs (`docs/multilingual-agents.md`) must be updated with the mode concept.

## Open questions
1. Does the Config service or the Conversation service own local model downloads? The Conversation service loads the models today; where the UI-facing status should come from changes the build.
