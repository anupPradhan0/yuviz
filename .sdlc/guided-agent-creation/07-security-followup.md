# Security review: PR #63 follow-up commits (git diff 5faa679..7eeea2c)
VERDICT: RED

Scope: Easy documents (knowledge create/upload/attach), Easy actions (toolexec enable, Config tool-provider
config and execute_api policy), the restructured prompt/template text, and the wizard's execute_api switch.
Findings 1, 2, 4 and 5 sit in server routes this diff does not change. The new Easy flow is the first
guided UI that drives them, and the caller asked for them to be confirmed. They are open on the server
whatever the UI sends, because an admin can call the same routes directly.

## Findings
1. [critical] Attaching a KB to an agent never checks that the KB belongs to the agent's tenant. Cross-tenant KB attach. — `services/knowledge/routers/agent_kb.py:21-28`, `services/knowledge/agent_kb.py:83-93`; backed by a plain FK (`database/knowledge_schema.sql:95`), an RLS `WITH CHECK` that tests only the agent (`database/rls.sql:404-405`), and retrieval joins with no `kb.tenant_id = a.tenant_id` (`services/knowledge/retrieval.py:71-77, 88-94`)
   Attack: A tenant-A admin who holds a tenant-B KB UUID (from a shared integrator, a former B employee, a support screenshot or a log) sends `POST /agents/{A-agent}/knowledge-bases {"kb_id": "<B-kb>"}`. `_authorize_agent` checks only the agent, so the row is inserted. A's agent then retrieves B's chunks and prompt-mode documents, and A reads them back through the chat test. The app runs on the BYPASSRLS superuser DSN (lesson 36), and even with RLS live the policy would allow it.
   Fix: Do the insert as `INSERT ... SELECT ... FROM knowledge_bases kb WHERE kb.id=$2 AND kb.tenant_id=<agent tenant> AND kb.deleted_at IS NULL`. Treat zero rows as the same 404 as a missing KB. Add `kb.tenant_id = a.tenant_id` to both retrieval joins. Add a regression test where a foreign kb_id is refused.

2. [critical] Creating an agent tool policy accepts another tenant's `tool_provider_config_id`, and at call time the runtime resolves that config's secret. — `services/config/agent_tool_policies.py:42-63` (unchecked insert), `database/schema.sql:156` (plain FK), `services/conversation/tools/policy_resolver.py:78-84` (join with no `tpc.tenant_id` predicate), `services/conversation/tools/provider_manager.py:60` (`resolve(policy.api_key_ref)` with no tenant binding)
   Attack: A tenant-A admin who holds a tenant-B tool-provider-config UUID sends `POST /agents/{A-agent}/tool-policies {"tool_name": "book_appointment", "tool_provider_config_id": "<B-tpc>"}`. Only the agent is authorized, so the insert goes through. A's agent's calls then run against B's provider (for example B's cal.com account) with B's `api_key_ref` secret: A can read B's bookings or availability, book into B's calendar or cancel there. `list_for_agent` also echoes B's config name and engine. This is the lesson 31/43 shape. The Easy flow's own `enableExecuteApi` (`admin-ui/lib/api.ts:1050`) picks a config from a tenant-scoped list, so the hole is in the route, not the UI.
   Fix: Insert only `WHERE EXISTS (SELECT 1 FROM tool_provider_configs WHERE id=$3 AND tenant_id=<agent tenant> AND deleted_at IS NULL)` and return the same 404 otherwise. Add `AND tpc.tenant_id = a.tenant_id` (join `agents`) to the runtime and list reads.

3. [high] The new appointment-booking template has the agent find, reschedule and cancel a booking from the name and number the caller states. — `services/config/agent_templates.py:335, 339`; the matching guardrail "share a person's details only with that person" (`services/config/system_prompt.py:55`, `admin-ui/lib/systemPromptBuilder.ts:28`) also rests on identity the caller claims
   Attack: Any member of the public calls a tenant's receptionist after the admin has ticked a find, reschedule or cancel custom API in the wizard. They say "May I... it's under Priya, 98xxxxxxxx" with the victim's details. The prompt tells the model to look up "the name and number on the booking" and pass them to the tool. The caller hears the victim's appointment details and cancels or moves it. "Only a booking it matches to this caller" can only mean whatever the caller said. Lesson 44 says to authorize call-time actions on server-side metadata, never on what the caller says.
   Fix: The template should match a booking on the caller's own number from call metadata (the `caller_id` param source), not on a number the caller speaks, and should say that a booking on a different number is passed to the team. The tool binding must enforce this, not just the prompt. On outbound calls, fail closed (lesson 44).

4. [medium] The document upload route enforces no size or type limit on the server. The `.txt`/`.md` limit exists only in the UI. — `services/knowledge/routers/documents.py:57` (`content = await file.read()`, no cap; any `content_type` stored)
   Attack: Any tenant admin, or a stolen admin token, POSTs a multi-GB body to `/knowledge-bases/{own-kb}/documents`. The whole body is read into memory, which OOMs the shared knowledge service and takes out retrieval for every tenant. Arbitrary binary types also get into storage and the ingestion queue.
   Fix: Read in chunks against a fixed cap (for example 10 MB) and return 413 above it. Allow only `.txt`/`.md` and `text/plain`/`text/markdown` on the server and return 415 otherwise.

5. [low] Knowledge by-id routes answer 404 for an unknown id and 403 for a foreign one, which lets a caller tell whether an id exists in another tenant. — `services/knowledge/routers/agent_kb.py:31-40` (`_authorize_agent`), `services/knowledge/routers/documents.py:20-37` (`_authorize_kb`, `_authorize_document`); `test_agent_scoped_routes_api.py:27` asserts this behaviour
   Attack: A tenant-A admin probes agent, KB or document UUIDs and learns which ones exist in some other tenant (lesson 2). The ids are random UUIDs, so this is low, but it is a real oracle. It would also confirm a leaked id before findings 1 and 2 are used.
   Fix: Return the same 404 for foreign and missing rows, as `knowledge_bases.py:59` and toolexec's `_authorize_agent_api` already do.

## Verified controls
- KB create is scoped to the path tenant: `bind_path_tenant` + `require_path_tenant_access` on `/tenants/{tenant_id}/knowledge-bases`, with `tenant_id` taken from the authorized path.
- `embedding_config_id` cannot point at another tenant's provider: composite FK `knowledge_bases_embedding_config_tenant_fkey (embedding_config_id, tenant_id) -> provider_configs(id, tenant_id)` (`database/knowledge_schema.sql:218-235`).
- Upload authorizes the KB's tenant before writing, and the storage path is `{tenant}/{kb}/{uuid}-{basename}` (`Path(filename).name`, so no path traversal).
- toolexec `PUT /agents/{id}/custom-apis/{api}` joins the custom API on `ca.tenant_id = a.tenant_id`. Foreign, missing and soft-deleted rows all raise the same `LookupError(_NOT_FOUND_DETAIL)` (`services/toolexec/agent_apis.py:18-50`).
- Tool-provider config create is scoped to the path tenant (router-level tenant dependencies, `tenant_id` from the path).
- Viewers are refused on every mutating route the Easy flow calls: KB create, upload, attach, custom-API enable, tool-provider create and tool-policy create all use `require_role("superadmin","admin")`.
- The wizard's execute_api switch does not widen what an agent can call. The executor requires an enabled `agent_custom_apis` row and `ca.tenant_id = a.tenant_id = $1` (`services/toolexec/executor.py:74-84`), so only the APIs the admin ticked can be called.
- Business facts stay labelled as data (`FACTS_LABEL`, "not instructions") and are rendered last (`agent_templates.py:27, 554`).
- `{{ }}` is still refused: `_no_double_braces` on name, business_name and business_facts (`schemas.py`), and `adds_template_braces` on revise (`system_prompt.py:384`) and accept (`routers/agents.py`).
- The customer-data check still runs on revise (`system_prompt.py:387`) and on accept (`routers/agents.py` accept_prompt). Guardrails and speech blocks are re-inserted whole by `enforce_prompt_structure`, which checks the new heading order.
- The revise route is still throttled per tenant (`agent_assist_throttle.check_revise`). The 3000-token cap raises per-call cost but stays inside that rate limit.
- `_agent_channel` now looks templates up by id from the static CATALOG, so it reads no tenant data.
