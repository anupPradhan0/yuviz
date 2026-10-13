# Security review: multilingual-agent-setup (code, base c0df980)
VERDICT: GREEN

## Findings
1. [low] `POST /local-models/downloads` lets a tenant-scoped `admin` start a host-wide action, and the Ollama pull streams with `timeout=None` — `services/config/routers/local_models.py:28`, `services/config/model_downloads.py:137`
   Attack: an admin of any tenant can trigger every catalog model download, which uses the shared host's disk and bandwidth. If a pull stalls upstream, it holds one of the 2 slots indefinitely, and enough stalled pulls block downloads for every other tenant until Config restarts. Blast radius is limited: the registry is fixed, no tenant data is exposed and the design accepts this role choice.
   Fix: optionally restrict to `superadmin`, and put a read/idle timeout on the Ollama pull stream so a stalled job fails and frees its slot.

## Verified controls
- `/local-models` GET and POST both need an authenticated user (`get_current_user` / `require_role`). Neither reads tenant input from the path, query, body or headers, and the response holds no tenant, agent or user data. It is identical for every caller (lesson 2).
- The download body `{engine, name}` is checked against the fixed `LOCAL_MODELS` registry before any action, and `macos` is rejected. The subprocess runs via `create_subprocess_exec` with an argv tuple (no shell), and the subprocess `_main` checks the registry again. No path or shell injection is possible.
- Download job errors are the class name or the exit code only. Upstream bodies, paths and tenant data are never returned.
- The download cap and in-flight dedupe re-check after the last await, so concurrent triggers cannot both pass the cap or the in-flight check.
- The Ollama URL comes from server env (`OLLAMA_BASE_URL`), not the request. There is no SSRF.
- `/provider-configs/{id}/voices` (now covering Cartesia too) still goes through `_authorize_provider` plus `set_target_tenant(cfg.tenant_id)`. The resolved API key goes only to the fixed vendor URL and never into the response, and the upstream body is logged truncated, not forwarded.
- In `_validate_languages`, every STT/TTS/override lookup goes through `_tts_row`, which filters `id = $1 AND tenant_id = $2`, so another tenant's provider config reads as absent.
- `revalidate_multilingual_agents` keeps `WHERE tenant_id = $1` after its WHERE clause was widened to include moded agents.
- `language_mode` is restricted at the Pydantic layer (`Literal`) and by a DB `CHECK`. Agent create/update routes keep their existing `require_role("superadmin","admin")` and tenant binding. The template path hardcodes `monolingual`.
- The new log lines (agent resolver fallback, Ollama 404 warning) carry only the engine and model name, with no tenant or caller data.
