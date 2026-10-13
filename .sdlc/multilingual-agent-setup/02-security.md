# Security review: .sdlc/multilingual-agent-setup/02-design.md
VERDICT: GREEN

## Findings
1. [low] `POST /local-models/downloads` lets any tenant-scoped `admin` start host-wide downloads into the `hf-cache` volume and Ollama that every tenant shares. The only limits are the bounded registry and 2 concurrent jobs, with no disk-free check and no retry limit after `failed`. (Design: Interfaces, `ModelDownloads` decisions.)
   Attack: a tenant-B admin triggers every `LOCAL_MODELS` entry in turn, and re-triggers any that fail. This uses up disk and bandwidth on the host that serves every tenant's live calls, through an action that has nothing to do with their own agents.
   Fix: before spawning, check free disk space against a headroom figure and return 507/429 when it is short, and/or rate-limit triggers per tenant. If the platform operator should decide what lands on the host, limit the route to platform-scoped callers.

## Verified controls
- `GET /local-models` uses `Depends(get_current_user)`, so the CONSOLE_ROLES fence keeps supervisor/agent roles out. It reads no tenant input, and its body contains no tenant, agent or user data.
- The download trigger uses `require_role("superadmin","admin")` (`services/config/deps.py:69`), so viewers get 403. It writes nothing to the DB, so there is no tenant row to scope.
- Download targets come only from the `LOCAL_MODELS` registry. Subprocess argv and the Kokoro `voices/{name}.pt` path are taken from the registry entry, never from the request string, so there is no path or argument injection.
- Ollama pull and status use only the env `OLLAMA_BASE_URL`, never a provider config's `extra.base_url`, so there is no new SSRF surface.
- Stored and returned download errors are only an exception class name or exit code: no paths, tenant or caller data. The new resolver and Ollama-404 log lines carry engine and model only (lesson 33).
- `revalidate_multilingual_agents` (`services/config/agents.py:398-452`) stays bound to `WHERE tenant_id = $1` with the edited row's tenant. Widening the WHERE to moded agents does not cross tenants, and its error names only an agent in the caller's own tenant.
- The Cartesia voice listing reuses `/{provider_id}/voices`, behind `_authorize_provider` (tenant check before use) and `set_target_tenant`. The resolved key goes only to the fixed `api.cartesia.ai` host and is never returned. Pointer refs (`env:`/`k8s:`) are already gated by `allow_pointer_schemes` at write time.
- `language_mode` is a Literal on the API plus a DB CHECK, and travels through the existing tenant-scoped agent create/update paths. Template creation passes a fixed mode, not caller input.
- The concurrency control is an atomic post-await re-check and insert on the event loop (no await inside). The design says it is per-process only, and the test plan covers it with gathered triggers.
