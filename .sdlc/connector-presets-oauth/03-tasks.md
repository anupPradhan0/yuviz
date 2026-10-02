# Tasks: Connector Presets + Per-Tenant OAuth2 (Authorization Code)

Design: `.sdlc/connector-presets-oauth/02-design.md` (security round 5, GREEN). Read `.sdlc/lessons.md` first.
Do not touch `.sdlc/current` or `.claude/worktrees/`.

Ordering rule: if the batch stops after any task, the system must refuse rather than permit. Where a task makes a parameter required, or adds a column or source a later task depends on, that task says what fails in the half-applied state.
Phases 2 and 3 are all-or-nothing checkpoints. Mid-phase the app may 500 (TypeError or 400) on credential writes, which is fail-closed. Do not deploy between their tasks.
Three round-5 low findings are folded in: T3 (seed call site), T13 (quarantine ceiling), T16 (disconnect side-channel).

## Phase 1: Foundations (additive; nothing reads them yet)

- [x] T1 Add tenant-bound ciphertext primitives — `libs/config_sdk/secrets.py`, `libs/config_sdk/tests/test_secrets_tenant_bound.py` (new, or extend the existing secrets tests) — MUST PRECEDE T9, T12, T15. Add `TENANT_BOUND_PREFIX="enc:t1."`, `is_tenant_bound`, `encrypt_tenant_secret(tenant_id, plaintext)`, `decrypt_tenant_secret(tenant_id, ref)` and `SecretTenantMismatch(ValueError)`.
  - Key: HKDF-SHA256 over the decoded `SECRET_ENCRYPTION_KEY`, `info=b"yuviz/enc-t1/tenant-bound"`. Cipher: AES-256-GCM with a 12-byte random nonce.
  - AAD: `b"yuviz:enc:t1|tenant:" + str(uuid.UUID(str(tenant_id)))`. Accept `str` or UUID. A non-UUID raises `ValueError`.
  - Open: a non-`enc:t1.` ref raises `ValueError`. A bad tag or bad base64 raises `SecretTenantMismatch("credential_ref_outside_tenant_namespace") from None`. No message contains the ref. Empty plaintext raises.
  - `decrypt_secret()` raises `ValueError` on an `enc:t1.` ref BEFORE it tries Fernet, so the shared `CompositeSecretResolver` refuses tenant-bound refs. `encrypt_secret` and legacy `decrypt_secret` are otherwise unchanged.
  - Done when:
    - A seal for A opens for A as `str` and as an asyncpg UUID.
    - Opening for B raises `SecretTenantMismatch`, and so does a flipped byte.
    - `decrypt_secret("enc:t1.…")` raises, and a legacy Fernet round-trip still passes.
    - Mutation check: with the AAD replaced by a constant in a scratch run, the "opens for B" test goes red. Report that it was seen to fail.

- [x] T2 Apply the schema and RLS changes — `database/schema.sql`, `database/rls.sql` — MUST PRECEDE everything below. Add `oauth_connections` and `oauth_authorization_states`, copied verbatim from the design's Data section.
  - Preserve the tenant-scoped partial unique index. The `provider_sub` index must stay non-unique and not tenant-scoped.
  - `custom_apis`: add `oauth_connection_id`, `preset_key`, `response_transform`, `idempotency_body_field`, `confirmation_template`, `session_send_cap` and the `idx_custom_apis_tenant_preset` index.
  - `custom_api_params`: add `body_path`, `value_prefix` and `value_digits_only BOOLEAN NOT NULL DEFAULT false`. `value_digits_only` is the column the `caller_id` recipient param needs (T22).
  - One `DO $$` block each (lessons 10, 13) for these constraints:
    - `custom_api_params_source_check` and `custom_api_params_source_shape`, widened with `'caller_id'`.
    - `value_prefix_shape` and `value_digits_shape`.
    - `auth_scheme_check`, `oauth_connection_shape`, the composite FK `(oauth_connection_id, tenant_id)`, `confirmation_shape` and `send_cap_shape`.
  - Add `custom_apis_auth_config_enc_bound` as `NOT VALID`, guarded by `IF NOT EXISTS`, so a re-apply never un-validates it.
  - Widen the `api_chain_runs` and `api_chain_steps` status CHECKs with `'confirmation_required'`. Copy the existing value lists verbatim from the CREATE TABLEs.
  - Update the inline CHECKs for fresh DBs (`:953-964`, `:847-867`).
  - In `rls.sql`: add two tenant-isolation policies copied from `custom_apis_tenant_isolation`, and add `('oauth_connection','oauth_connections')` to the audit backfill list.
  - Done when:
    - On a DB seeded with live `custom_apis` and `custom_api_params` rows, applying `schema.sql` TWICE with plain `psql -f` leaves every named constraint in `pg_constraint`.
    - `custom_apis_auth_config_enc_bound` has `convalidated=false` and rejects a new legacy `enc:` insert.
    - A `custom_apis` row for tenant B referencing tenant A's connection id fails on the FK.
    - Say explicitly that the seeded DB is what lets the guard trip (lesson 12).

## Phase 2: Config credential tables, four-table pair (all-or-nothing)

- [ ] T3 Make `resolve_api_key_input` fail closed and repair its callers in the provider and tool-provider paths — `services/config/provider_configs.py`, `services/config/tool_provider_configs.py`, `scripts/seed_default_config.py` — MUST PRECEDE T4, T5, T6.
  - `provider_configs.py`:
    - Add `STORED_SENTINEL`, `mask_enc`, `ref_mask_required(user)` and `public_provider_config(cfg, *, masked)`.
    - `mask_enc` maps `enc:…` to `"[stored]"` and `"quarantined"` to `""`. `env:`, `k8s:` and `None` are unchanged.
    - New signature: `resolve_api_key_input(api_key, api_key_ref, *, current_ref=None, allow_pointer_schemes: bool)`. `allow_pointer_schemes` is REQUIRED with NO DEFAULT.
    - Rules:
      - `api_key == "[stored]"` raises.
      - `api_key_ref == "[stored]"` returns `current_ref`, or raises when `current_ref` is None.
      - An `enc:` ref is accepted only if it `== current_ref`.
      - Any other `enc:` raises `ValueError("credential_ref_not_accepted: send the key as api_key")`.
      - `env:`/`k8s:` raise the SAME message when `allow_pointer_schemes` is false. The text must not differ by scheme.
    - `create_provider_config` and `update_provider_config` gain a required `allow_pointer_schemes` keyword and pass it down. `update_provider_config` moves the helper call to directly after the `FOR UPDATE` fetch and passes `current_ref=old["api_key_ref"]`.
    - Rewrite the module docstring and the `:27-30` note.
  - `tool_provider_configs.py`: the same required keyword on create (`:84`) and update (`:122`). Update reads the old row first and passes `current_ref`.
  - `scripts/seed_default_config.py` is the fifth, unenumerated call site (`:103` update, `:108` create, reaching the helper via the service functions). FIX THE CALL SITES. Pass the literal `allow_pointer_schemes=False` at both. The seed supplies no key and no ref, so least privilege is correct. Do NOT add a default to the shared helper or to the service-function signatures: that would silently undo the pointer-scheme fix. Add a code comment saying so.
  - Update every existing caller in `services/config/tests/test_provider_secret_input.py` and `tests/` to pass the keyword. Add cases for each rule above.
  - Done when:
    - `resolve_api_key_input(None, "x")` without the keyword raises `TypeError`.
    - `python scripts/seed_default_config.py` runs twice against a fresh DB, creating then updating without error.
    - A mechanical grep-based test asserts every call of `resolve_api_key_input` in the repo passes `allow_pointer_schemes=` and asserts the call count. It must include the seed script, and must go red if the seed keyword is removed.
    - Mid-phase state: routers not yet passing the keyword raise `TypeError` on writes (refusal). That is accepted until T6.

- [ ] T4 Apply the same rules to carriers, the fourth ref table — `services/config/carriers.py`, `services/config/schemas.py`, `services/config/audit.py`.
  - `carriers.py`:
    - `create_carrier` and `update_carrier` gain a required `allow_pointer_schemes` keyword and a plaintext `auth_token`.
    - They run `auth_token_ref = resolve_api_key_input(auth_token, auth_token_ref, current_ref=<old row's auth_token_ref on update, None on create>, allow_pointer_schemes=…)`.
    - `update_carrier` first reads the old row under `SELECT … FOR UPDATE` in the same transaction.
    - Add `public_carrier(row, *, masked)`.
    - Replace the docstring claim (`:8-10`) that returning `auth_token_ref` is fine.
  - `schemas.py`: add `auth_token: SecretStr | None = None` to `CarrierCreate` and `CarrierUpdate`.
  - `audit.py`: add `api_key`, `auth_token` and `credentials` to `_SECRET_REF_FIELDS`.
  - Update the existing `tests/test_cross_tenant_admin.py` `create_carrier(...)` callers (`:363,365,698,718,748,763,780`) and the carrier tests to pass the keyword.
  - Done when:
    - A carrier created from plaintext `auth_token` stores `enc:`.
    - Pasting another tenant's ciphertext on create or update raises `credential_ref_not_accepted`.
    - A byte-identical round-trip and `"[stored]"` both keep the stored value.
    - An audit row for a carrier create contains no plaintext token.

- [ ] T5 Apply the rules to telephony credentials — `services/config/telephony_configs.py`, `services/config/tests/test_telephony_configs.py`.
  - `_normalize_credentials(provider, credentials, old_credentials=None, *, allow_pointer_schemes: bool)` and `_normalize_one(field_name, entry, old_entry, *, allow_pointer_schemes: bool)`. The keyword is required.
  - For a list field, `old_entry` is the old list's entry at the same index.
  - `"[stored]"` maps to `old_entry`, or raises when there is none. An `enc:` entry is kept only if `== old_entry`; otherwise it raises `ValueError(f"{field_name}: credential_ref_not_accepted")`.
  - `env:`/`k8s:` entries raise when `allow_pointer_schemes` is false.
  - `create_telephony_config` (`:226`) passes no old credentials. `update_telephony_config` (`:292`) passes `old["credentials"]`, JSON-decoded if it is a `str`.
  - Add `public_telephony_config(cfg, *, masked)`. It is registry-independent and masks every `enc:` string, scalar or list entry, including `native` rows.
  - Rewrite the docstring (`:7-13`).
  - Update the existing `_normalize_credentials` calls in the tests to pass the keyword.
  - Done when:
    - Pasting A's `credentials.auth_token` or `api_keys=[A's]` onto B gives `credential_ref_not_accepted`, and B's row is unchanged.
    - `api_keys: ["[stored]"]` round-trips.
    - `public_telephony_config(masked=True)` leaves no `enc:` string in a `native` row.
    - Call sites that omit the keyword raise `TypeError`.

- [ ] T6 Wire the four Config routers: literal pointer predicate and response masking — `services/config/routers/provider_configs.py`, `routers/tool_provider_configs.py`, `routers/telephony_configs.py`, `routers/carriers.py` (four small, mechanical edits, one pattern). Requires T3, T4, T5.
  - Every create and update route passes the literal `allow_pointer_schemes=is_platform_scoped(current_user)`. It must be this exact expression, `tenant_id is None`, not a role test (lesson 24, lesson 32).
  - Every route that returns a row wraps it:
    - `public_provider_config(..., masked=ref_mask_required(current_user))`
    - `public_telephony_config(..., masked=ref_mask_required(current_user))`
    - `public_carrier(..., masked=ref_mask_required(current_user))`
  - This covers list, get, create, update, list-by-provider and set-default-outbound.
  - Router guards are unchanged. Service getters and the cache keep the sealed value.
  - Done when:
    - A tenant admin posting `env:X` or `k8s:/x` to each of the four tables gets 400 with the same text as a rejected `enc:`.
    - A platform-scoped principal can store the pointer.
    - A viewer's GET shows `"[stored]"`.
    - The Conversation service account (`is_service_account` AND `tenant_id IS NULL`) still reads the sealed `enc:`.

- [ ] T7 Add the route-walking masking and call-site tripwire tests — `services/config/tests/test_credential_masking.py` (new), `services/config/tests/test_carriers.py`. Requires T6.
  - Walk `app.routes` mechanically (lesson 29) for every route returning a row from the four tables.
  - For each of superadmin, admin, supervisor and viewer, assert no string in the body starts with `enc:` and the credential fields equal `"[stored]"`.
  - Assert the walked-route count, so a new route breaks the test.
  - Run one case as the platform service account and assert it still gets `enc:`. This proves the fixture contains `enc:` and the mask is not over-broad.
  - The tripwire greps the call sites of `resolve_api_key_input`, `_normalize_credentials`, `create_carrier` and `update_carrier` and asserts the keyword and the count.
  - Done when the tests pass, and with the `public_carrier` wrap deleted in a scratch run the role cases go red.

- [ ] T8 Make the admin UI handle masked values — `admin-ui/components/SecretRefInput.tsx`, `admin-ui/app/(console)/telephony/page.tsx`. MUST LAND IN THE SAME RELEASE as T6 (lesson 11). Read `admin-ui/AGENTS.md` first.
  - `SecretRefInput.tsx`:
    - `isStored` also returns true for `"[stored]"`.
    - `secretPayload` gets a first rule, `if (isStored(v)) return { api_key_ref: v }`, placed before the scheme regex at `:162`.
  - `telephony/page.tsx`:
    - Edit-form credential inputs (`:976-1008`) and the carrier `auth_token_ref` input render empty with the placeholder "Stored — type to replace".
    - An untouched field submits `"[stored]"`. A typed value is sent as plaintext (`auth_token` for carriers).
    - The read-only `mask()` view is unchanged.
  - Done when:
    - A unit test asserts `secretPayload("[stored]","[stored]")` equals `{api_key_ref:"[stored]"}` and never `{api_key:…}`.
    - In a browser (lesson 23), editing a Vobiz config and a carrier without touching the secret saves with 200 and the credential still works.

## Phase 3: Toolexec tenant-bound refs and the quarantine pair (all-or-nothing)

- [ ] T9 Refuse legacy and pointer refs in the toolexec reader and writer — `services/toolexec/auth_schemes.py`, `services/toolexec/tests/test_auth_schemes_tenant_bound.py` (new). Requires T1.
  - Add `class ReconnectRequired(ValueError)`.
  - `validate_tenant_ref`:
    - The `enc:` branch accepts only `is_tenant_bound(ref)` AND `decrypt_tenant_secret(tenant_id, ref)` succeeding, with the result discarded.
    - `env:` and `k8s:` are rejected outright at write time.
    - Replace the "enc: always allowed" docstring.
  - `resolve_tenant_ref`:
    - It decrypts `enc:` only via `decrypt_tenant_secret(tenant_id, …)`, never through `_tenant_secret_resolver`.
    - It raises `ValueError("credential_ref_outside_tenant_namespace")` for a legacy `enc:`.
    - The `env:`/`k8s:` READ branch stays intact so live rows do not break before T12.
  - Done when:
    - A tenant-bound ref for A, written under B, fails at write time.
    - A row inserted directly with a legacy Fernet ref, or with A's ref under B, executes to `credential_unavailable` with zero upstream requests recorded by the mock.
    - A direct `env:` row still resolves until quarantined.
    - Half-applied state: legacy rows fail closed, never decrypt under the wrong tenant.

- [ ] T10 Seal on write and mask on read for custom-API credentials — `services/toolexec/custom_apis.py`, `services/toolexec/schemas.py`, `services/toolexec/routers/custom_apis.py`, tests. Requires T9. Land with T11.
  - `custom_apis.py`:
    - Add `_seal_auth_secrets(tenant_id, auth_scheme, auth_config, auth_secrets, old_auth_config)`, run before the unchanged `_validate_credential_ref`.
    - Per field in `_CREDENTIAL_REF_FIELDS[scheme]`: `auth_secrets` wins, and both a value and a secret raises `credential_ref_ambiguous`. `"[stored]"` is copied from the old config only on update with an unchanged scheme and an existing field. Anything else raises `credential_ref_not_a_reference`. An unknown `auth_secrets` key raises.
    - Add `"oauth2_authorization_code": ()` to `_CREDENTIAL_REF_FIELDS`.
    - Add `public_custom_api(api)`: every `*_ref` becomes `"[stored]"`, and `"quarantined"` becomes `""`. This includes pointers.
  - `schemas.py`: `CustomApiCreate`/`CustomApiUpdate` gain `auth_secrets: dict[Literal["key_ref","token_ref","client_id_ref","client_secret_ref"], SecretStr] | None`. Leave the `auth_scheme` Literals unchanged.
  - `routers/custom_apis.py`: list (`:79-82`), get (`:113`), create and update return `public_custom_api(...)` and pass `auth_secrets` through.
  - Done when:
    - Create with `auth_secrets` stores an `enc:t1.` ref that opens for that tenant (read back from the DB).
    - PATCH with `"[stored]"` leaves the stored string byte-identical.
    - `"[stored]"` on create, or with a changed scheme, gives 400.
    - A viewer's GET body contains no string starting with `enc:`, `env:` or `k8s:`.
    - Pasting A's real `key_ref` on B's `bearer` row gives 400 `credential_ref_outside_tenant_namespace`.
    - A route-count tripwire covers the custom-API dict-returning routes.

- [ ] T11 Make the Custom APIs panel use `auth_secrets` — `admin-ui/components/CustomApisPanel.tsx`, `admin-ui/lib/toolexecApi.ts`. Requires T10, T8.
  - In the auth form (`:238-243`), a pasted key goes out as `auth_secrets[<field>]`. A scheme-shaped value goes as the ref, via `secretPayload`. `"[stored]"` is sent back unchanged. An empty quarantined field shows as required.
  - `toolexecApi.ts`: add `auth_secrets` to the create and update payloads, and add `preset_key` and `oauth_connection_id` to `CustomApi`.
  - Done when, in a browser, saving an unrelated edit to a `bearer` API keeps its credential working, and a stored credential shows hidden with Replace.

- [ ] T12 Write the re-encryption and quarantine script, with the `_CONFIG_REF_COLUMNS` pair — `services/toolexec/reencrypt_tenant_refs.py` (new), `services/toolexec/tests/test_reencrypt_tenant_refs.py` (new). Requires T1, T2, T10, and the Config work in T3-T6 so no new copies form. MUST PRECEDE T13.
  - Declare `_CONFIG_REF_COLUMNS` once as `(table, column, kind)` tuples for `provider_configs.api_key_ref`, `tool_provider_configs.api_key_ref`, `telephony_configs.credentials` (`jsonb_credentials`) and `carriers.auth_token_ref`. BOTH the scan and the quarantine writes iterate that one tuple.
  - Transaction:
    - The first statement is `LOCK TABLE custom_apis, provider_configs, tool_provider_configs, telephony_configs, carriers IN SHARE ROW EXCLUSIVE MODE`. It runs under the superuser DSN.
    - Scan legacy `enc:` (not `enc:t1.`) in `custom_apis.auth_config` (`key_ref`, `token_ref`, `client_id_ref`, `client_secret_ref`, soft-deleted rows included) and in the four Config columns.
    - Group by exact ciphertext string. REBIND only when every occurrence across all five columns is one non-NULL tenant: `decrypt_secret`, then `encrypt_tenant_secret(row tenant, …)`, then the conditional `UPDATE … WHERE id=$1 AND tenant_id=$2 AND auth_config->>$3=$4`.
    - Zero rows aborts the transaction.
    - QUARANTINE, writing the literal `"quarantined"` (not NULL), in EVERY occurrence, original holder included, when a ciphertext spans 2+ tenants, sits on a platform `provider_configs` row, or fails to decrypt. Each write is a conditional `UPDATE … WHERE id=$1 AND <col>=$2`.
    - Quarantine `env:`/`k8s:` in `custom_apis.auth_config` only. Leave Config pointers untouched.
    - One `audit.write_audit` row per occurrence on its own tenant, with reason `credential_quarantined_shared_ciphertext`, `…_undecryptable` or `…_pointer_ref`. Platform rows appear in the report only.
    - Then `VALIDATE CONSTRAINT custom_apis_auth_config_enc_bound` in the same transaction.
  - Interface: `reencrypt(dsn, *, dry_run=False) -> (counts, report_rows)`. `counts` has `rebound`, `quarantined_custom_apis`, `quarantined_pointer_refs`, `quarantined_config{4}`, `remaining` and `config_shared`.
    - `main` supports `--dry-run` and `--report <path>`. The report is JSON Lines, 0600, written only AFTER commit, ids only.
    - It exits 1 unless `remaining == 0 AND config_shared == 0`. stdout is counts only.
  - Tests run against a DB SEEDED with live legacy rows. Seed all the design's cases:
    - unique A refs, live and soft-deleted;
    - an A/B shared ref;
    - a platform-copied ref;
    - an undecryptable ref;
    - A's telephony and tool-provider ref copied into a B `custom_apis` row;
    - the telephony A/B pair with no `custom_apis` occurrence;
    - both carrier cases;
    - an `env:` ref in `custom_apis` and an `env:` ref in Config;
    - a single-tenant Config ref that must survive byte-identical.
  - Done when the tests listed in design test 13 pass, including:
    - the inventory tripwire comparing `_CONFIG_REF_COLUMNS` (as a set AND by length) against an `information_schema.columns` query, asserting the computed set rather than a literal;
    - removal of a table from `_CONFIG_REF_COLUMNS` in a scratch run turning the rebind tests red;
    - the config-quarantine-disabled scratch run giving `config_shared != 0` and exit 1;
    - a skipped-row monkeypatch rolling back all five columns with no report file and exit 1;
    - `services/telephony/accounts.py` and `services/did/provider_manager.get()` RAISING on quarantined rows (this fails if the script wrote NULL);
    - `convalidated=true` after the run, and a second run changing nothing.
  - Half-applied state: until it runs, legacy refs fail closed (T9).

- [ ] T13 Add a blast-radius ceiling to the quarantine transaction (round-5 low 3) — `services/toolexec/reencrypt_tenant_refs.py`, `services/toolexec/tests/test_reencrypt_tenant_refs.py`. Requires T12.
  - After the scan and classification, and BEFORE any rebind, quarantine or VALIDATE write, compute the planned quarantine set.
  - Abort (roll back, exit code 2, a message naming counts only) if EITHER of these holds:
    - the planned set exceeds N rows (propose `--max-quarantine`, default 25), or exceeds N% of all ref-bearing rows in the scanned tables (propose `--max-quarantine-pct`, default 10);
    - the `"undecryptable"` class alone exceeds a small absolute floor (propose 3). That is the wrong-`SECRET_ENCRYPTION_KEY` signature, which would otherwise null the platform.
  - Also abort if the set includes any platform (`tenant_id IS NULL`) `provider_configs` row, unless overridden.
  - An explicit `--allow-large-quarantine` flag overrides, and the override is logged by count.
  - `--dry-run` must report whether the ceiling would trip. Defaults are named constants and are documented in T26.
  - Done when:
    - Seeding 40 copies of one tenant's own ciphertext (the viewer-plants-copies scenario) aborts with exit 2 and every row byte-identical, and `convalidated` stays false.
    - The same seed with `--allow-large-quarantine` proceeds.
    - A run under a deliberately wrong key aborts on the undecryptable floor rather than nulling the table.
    - A normal small seed (T12's) still passes with no flags.
    - No exception handler or `|| true` turns the abort into a success exit (lesson 14).

## Phase 4: OAuth core (the disabled-by-default connector surface)

- [ ] T14 Move the transport and quiet the httpx loggers — `services/toolexec/custom_apis.py`, `services/toolexec/executor.py`, `services/toolexec/__main__.py`, plus a test. Requires T10.
  - Move `PinnedResolverTransport` (`executor.py:287-309`) into `custom_apis.py`. `executor` re-imports it under the same name. `_step_transport` and its monkeypatch seam stay in `executor`.
  - Extract `configure_logging()` in `__main__.py`: the existing `basicConfig`, plus `httpx` and `httpcore` set to WARNING. `main()` calls it.
  - Done when:
    - The existing executor tests are green.
    - A test calls `configure_logging()` with caplog at INFO, runs a request whose URL path carries a sentinel, and asserts zero `httpx`/`httpcore` records.
    - That test goes red with the WARNING lines removed. Restore the logger levels in teardown.

- [ ] T15 Add the OAuth provider registry and the authorize/redeem flow — `services/toolexec/oauth.py` (new), `services/toolexec/tests/test_oauth.py` (new). Requires T1, T2, T9, T14. MUST PRECEDE T16.
  - Provider registry (Google, Microsoft, Zoho), with `configured_providers()` hiding any provider whose env is unset. `client_secret` resolves via a module-level platform `CompositeSecretResolver`.
  - `start_authorization`:
    - Deletes this tenant's expired states.
    - Generates `state` and a PKCE S256 verifier.
    - Stores `sha256(state)` and `encrypt_tenant_secret(tenant_id, verifier)`.
    - Scopes come only from the registry, presets and existing connection scopes.
  - `complete_authorization`:
    - Redeem with the single conditional `UPDATE … WHERE tenant_id=$1 AND state_hash=$2 AND user_id=$3 AND consumed_at IS NULL AND expires_at>now() RETURNING …`.
    - Zero rows raises `ValueError("oauth_connection_failed")` and no token request is built.
    - Run the exchange AFTER the commit.
    - Every provider call goes through `resolve_and_validate_endpoint`, a pinned `_provider_transport` and a 10s timeout.
    - Secrets go ONLY in `data=` (a form body), with no `params=` and no query string.
    - Re-raise as a bare `ValueError` `from None`.
    - Require a refresh token.
    - Read `account_label` and `provider_sub` from the same response.
    - Validate the Zoho `accounts_server` against the allowlist.
    - Upsert with the design's single `ON CONFLICT (tenant_id, provider) WHERE deleted_at IS NULL` statement, which preserves the connection id on reconnect. Then write an audit row.
  - Every statement carries `tenant_id = $1`.
  - Done when design tests 2 and 5 (the state and token sink parts) pass:
    - forged, replayed, expired, other-admin and cross-tenant states each give `oauth_connection_failed` with a RECORDED ZERO token requests;
    - stored refs read back as `enc:t1.` and `decrypt_tenant_secret(B, ref)` raises;
    - every recorded request has `url.query == b""`;
    - with the tenant predicate deleted in a scratch run the isolation test goes red.

- [ ] T16 Add token access, refresh, and a side-channel-free disconnect — `services/toolexec/oauth.py`, `services/toolexec/auth_schemes.py`, `services/toolexec/tests/test_oauth.py`. Requires T15. Includes round-5 low 2.
  - `access_token_for`:
    - Read with `WHERE tenant_id=$1 AND id=$2 AND deleted_at IS NULL`.
    - No row, or a non-`connected` status, raises `ReconnectRequired`.
    - There is no in-process cache.
  - Refresh:
    - Use the conditional `UPDATE … WHERE tenant_id=$1 AND id=$2 AND refresh_token_ref=$3 AND status='connected' RETURNING *`.
    - Zero rows means re-read and use the winner's token, or raise `ReconnectRequired` if the re-read is not connected.
    - An `invalid_grant` flips the status with the same conditional predicate.
    - Network and 5xx errors raise a plain `ValueError` and leave the status unchanged.
  - `disconnect`:
    - Run the design's single `UPDATE … FROM (SELECT … FOR UPDATE)` statement. Zero rows raises `LookupError`, which becomes a fixed-body 404 that is identical for an absent id and another tenant's id.
    - Write an audit row.
    - AMENDMENT FOR LOW 2: the foreground path is that one DB write plus the audit write, and it returns a CONSTANT body `{"disconnected": true}` that does not depend on the revoke outcome or on any other tenant's state. Drop the `{"revoked": bool}` field.
    - The shared-grant `EXISTS` probe on `platform_conn` (`provider=$1 AND provider_sub=$2 AND status='connected' AND deleted_at IS NULL AND tenant_id<>$3`, with `$2` read from the disconnecting row), the revoke decision, and the revoke POST (form body, 10s timeout) all run in a Starlette `BackgroundTasks` callback AFTER the response is sent.
      - This makes response time invariant to the shared-grant state, with no thread pool to shut down (lesson 26).
      - Swallow errors in the callback. Log only `oauth_revoke_skipped_shared_grant` with the connection id and no other tenant's data.
      - A NULL `provider_sub` means revoke anyway.
      - Record the contract change in the design's Risks section (or note it for the reviewer).
  - `auth_schemes.apply()`:
    - Add an `oauth2_authorization_code` branch that calls `oauth.access_token_for(api["tenant_id"], api["oauth_connection_id"])` through a function-local import.
    - Enforce the host binding: if `urlsplit(endpoint_url).hostname` is not in `provider.api_hosts`, raise `ValueError("credential_unavailable")` BEFORE the header is set.
    - Set `Authorization: Bearer …` and return `{"Authorization"}`.
    - Re-raise `ReconnectRequired` before the generic `except Exception`.
  - Done when design tests 1, 3 and 20 pass:
    - the 404 body is byte-identical for an absent id and another tenant's id from the SAME principal (lesson 2);
    - the disconnect response body is exactly `{"disconnected": true}` in the shared-grant, sole-holder, NULL-sub and different-account cases (test 20's `{"revoked": false}` assertions are updated to this);
    - the revoke mock sees ZERO calls in the shared case, and a call carrying B's old refresh token in the form body once B disconnects;
    - the foreground handler time does not include the revoke call (assert by making the revoke mock sleep);
    - the off-provider-host row gets `credential_unavailable` with zero requests to that host;
    - a fresh-interpreter import of `apply()` resolves the function-local import.

- [ ] T17 Mount the connector routes — `services/toolexec/routers/oauth_connections.py` (new), `services/toolexec/app.py`, `services/toolexec/tests/test_oauth_routes.py` (new). Requires T16.
  - Routes: `GET /oauth-providers`, `GET /tenants/{t}/oauth-connections` (explicit column list, no `*_ref`, no `provider_sub`), authorize, callback, and `DELETE …/{connection_id}`.
  - Each handler first awaits `await assert_tenant_access(...)`. Writes sit behind `require_role("superadmin","admin")`. Models use `extra="forbid"` and have no redirect field.
  - Every callback failure returns the same `400 {"detail":"oauth_connection_failed"}`.
  - Add the `LookupError` 404 handler. Register `PresetConnectorRequired` handlers later in T24.
  - Done when:
    - Viewer and supervisor tokens get 403 on authorize, callback and disconnect (design test 9).
    - An AST check confirms every `assert_tenant_access` call is awaited (lesson 38).
    - The connection object's exact key set is asserted, with no `provider_sub`.

## Phase 5: Presets, executor, conversation

- [ ] T18 Let `custom_apis` write preset-only fields and lock preset rows — `services/toolexec/custom_apis.py`, `services/toolexec/schemas.py`, tests. Requires T2, T10.
  - Extract the transaction body of `create_custom_api` into `_insert_custom_api(conn, …)` (create calls it).
    - It writes `oauth_connection_id`, `preset_key`, `response_transform`, `idempotency_body_field`, `confirmation_template`, `session_send_cap`, and per-param `body_path`, `value_prefix` and `value_digits_only`.
    - `_replace_params` writes the same param columns.
  - Add the `preset_managed` guard in `update_custom_api` directly after the `FOR UPDATE` fetch (`:586-590`): `400 preset_managed` before the advisory lock or any write. `soft_delete_custom_api` stays ungated.
  - `schemas.py`: add `OAuthAuthorizeRequest`, `OAuthCallbackRequest`, `PresetApplyRequest` (a discriminated union per preset key, `extra="forbid"`, WhatsApp `api_key: SecretStr`) and `"confirmation_required"` on `ChainStepReport.status` and `ChainExecuteResponse.chain_status`.
    - `ChainExecuteRequest` gains three independent fields: `caller_number`, `called_number` and `call_direction`, all `str` (not `Literal`), default `""`.
    - Leave `CustomApiCreate`/`CustomApiUpdate` exposing none of `confirmation_template`, `session_send_cap`, `value_prefix`, `value_digits_only` or the `caller_id` source.
  - Done when:
    - PATCH on a `preset_key` row gives `400 preset_managed`.
    - `CustomApiCreate(auth_scheme="oauth2_authorization_code")` gives 422.
    - A hand-registered API round-trips unchanged.

- [ ] T19 Write the preset definitions and pure functions — `services/toolexec/presets.py` (new, pure part only), `services/toolexec/tests/test_presets.py` (new). Requires T18. MUST PRECEDE T20, T22, T23.
  - `PRESETS`: the five calendar rows, the three WhatsApp rows and the Sheets row, with the exact param tables from the design.
    - Every `caller_id` param has `sensitive=true`.
    - The WhatsApp rows have `session_send_cap=3` and a `caller_id` recipient: `value_digits_only=true` for Gupshup and Meta, and Interakt's `fullPhoneNumber` keeps the `+`.
    - Sheets uses literal `valueInputOption=RAW`.
    - `gcal_find_booking` has NO `q` param.
  - `normalize_ani` and `remote_party_number(call_direction, caller_number, called_number)`: inbound picks the caller, outbound picks the callee, every other direction value, an empty number, or an equal pair returns `None`.
  - `validate_response_transform` and `apply_response_transform` (`google_freebusy_slots`, `google_booking_lookup(caller_ani=…)`, `google_event_projection`), `render_confirmation(template, arguments_redacted, upstream_responses)` and `claim_release_target`.
  - `google_booking_lookup` keeps an item only if all four checks hold: status not cancelled, `yuviz_preset` equal, `yuviz_phone == caller_ani`, and the id parses as a UUID hex. It drops `summary`, `description`, `attendees` and the rest.
  - Done when:
    - The `remote_party_number` table test passes.
    - A test over `PRESETS` asserts no param is named `upstream`.
    - A test over `PRESETS` asserts every `caller_id` param is `sensitive`.
    - The lookup transform drops an event with a matching phone but no `yuviz_preset`, and one with a non-UUID id.
    - Planted PII sentinels do not survive the projection.

- [ ] T20 Add the executor argument-plumbing and idempotency changes — `services/toolexec/executor.py`, `services/toolexec/tests/` (extend the executor tests). Requires T14, T18, T19.
  - (b) `except auth_schemes.ReconnectRequired: raise _StepFailure("unavailable","reconnect_required")` immediately before the existing `except ValueError`.
  - (c) `_claim_side_effect` returns the claim row id (`uuid.UUID | None`). After a successful claim, if `idempotency_body_field` is set, `body_fields[field] = claimed.hex`.
  - (d) `body_path` nested placement, with JSON-encoding of non-scalar form values.
  - (g) Path values join the hashed arguments: `_resolve_arguments` records `path_values[name]` and returns it as the SEVENTH element.
    - Update the one caller's unpack, and initialise `path_values = {}` before the `try`.
    - Both `resolved_arguments` and `failure_arguments` are built from the literal `{**body_fields, **query_params, **headers, **path_values}`, each minus `injected_auth_keys`.
    - Keys are bare names, so `sensitive` redaction by name still applies.
  - (e) Apply `presets.apply_response_transform(...)` right after `json.loads` and before `redaction.redact`.
  - Re-read the whole enclosing functions after editing (lesson 35).
  - Done when design tests 6 and 8 pass:
    - the existing flat-body API produces a byte-identical outbound body and an unchanged `arguments_hash`;
    - `gcal_book`'s body `id` equals the hex of its claim row's id (read back from the DB);
    - a `sensitive` path param shows `[redacted]` in `arguments_redacted` yet changes the hash;
    - two cancels of different `event_id` hash differently;
    - `reconnect_required` surfaces as `unavailable`.

- [ ] T21 Plumb call direction and numbers through Conversation — `services/conversation/tools/types.py`, `services/conversation/tools/orchestrator.py`, `services/conversation/pipeline.py`, tests. Requires T18. MUST PRECEDE T22 and T24.
  - `types.py`: `ToolExecutionContext` gains `called_number: str = ""` and `call_direction: str = ""`.
  - `orchestrator.py`: `run_turn` gains the two keywords and passes them into the context (`:227-234`).
  - `pipeline.py`: the `run_turn` call (`:1486-1489`) passes `called_number=self._called_number, call_direction=self._direction`. Change the constructor default `direction: str = "inbound"` (`:458`) to `""`, so an omitting construction site fails closed. Check `transcript_builder` still maps `""` to `"inbound"`.
  - Done when:
    - A handler built with `direction="outbound", called_number=C` produces a context carrying both.
    - A handler built without `direction` produces `call_direction == ""`.
    - The existing conversation tests are green.
    - Mid-state is safe: `call_direction=""` makes every caller_id step fail closed (T22), because toolexec treats it as unknown.

- [ ] T22 Add the `caller_id` param source and the `_remote_party` gate — `services/toolexec/executor.py`, `services/conversation/tools/executors/api_exec_executor.py`, tests. Requires T2 (the `value_digits_only` column), T19, T20, T21. This is the pair: the `caller_id` recipient param needs the `value_digits_only` column. Verify both ends.
  - `api_exec_executor.py`: add `caller_number`, `called_number` and `call_direction` to the `/execute` body (`:64-76`), straight from `request.context`. Conversation never picks the remote party.
  - `executor.py`:
    - Add `_remote_party(tenant_id, request)`, which calls `presets.remote_party_number` and then rejects any number that is one of THIS tenant's own DIDs via `SELECT 1 FROM phone_numbers WHERE tenant_id=$1 AND '+'||regexp_replace(did,'\D','','g')=$2 LIMIT 1` (explicit predicate, UUID from `_resolve_tenant_uuid`).
    - `_remote_party` runs ONCE per chain, and only when the tree has a `caller_id` param or a `google_booking_lookup` transform.
    - `_resolve_arguments` gains the keyword `remote_party`, fed by exactly `remote_party = await _remote_party(...)`. `request.caller_number` is read nowhere else in `executor.py`.
    - A `source=="caller_id"` param resolves to `(value_prefix or "") + (remote_party.lstrip("+") if value_digits_only else remote_party)`, with `argument_sources[name]="caller_id"`.
    - `caller_arguments[name]` is never read for it.
    - A `None` remote party raises `_StepFailure("invalid_argument","caller_id_unavailable")` before any upstream request.
    - `apply_response_transform` receives `caller_ani=remote_party`.
  - Done when design tests 14 and 19 (recipient part) pass, each seen red under the stated mutation:
    - the model-supplied `destination` or `caller_phone` never reaches the mock;
    - the outbound call books under the CALLEE (`called_number`), not the campaign DID;
    - `""`, `"test"`, `"sideways"` and equal-number cases give `caller_id_unavailable` with zero upstream requests;
    - the mislabelled-inbound case with the tenant's own DID fails closed, and goes red with the `phone_numbers` check deleted;
    - `api_exec_executor` posts three distinct values, with swapped fields going red.
  - Half-applied state: an old Conversation sends no direction, so every `caller_id` step fails closed.

- [ ] T23 Add the confirmation gate, send cap, claim release and Conversation-side handling — `services/toolexec/executor.py`, `services/conversation/tools/executors/api_exec_executor.py`, `services/conversation/tools/policy_resolver.py`, tests. Requires T19, T20, T22. MUST PRECEDE T24.
  - `executor.py`:
    - (f) After `arguments_hash` and before `_claim_side_effect`: if `confirmation_template` is set and `_confirmed_in_prior_turn(...)` is false, raise `_ConfirmationRequired(presets.render_confirmation(template, arguments_redacted, upstream_responses))`.
      - Use the design's SQL, with the explicit `tenant_id`, the `r.turn_id <> $5` predicate, the 10-minute window and the `NOT EXISTS` retire clause.
      - Persist the step and finalize the run as `confirmation_required`. The response carries `deterministic_response`, `data={}` and `error="confirmation_required"`.
      - An unrenderable template raises `_StepFailure("failed","confirmation_unrenderable")`.
    - (k) Immediately after the gate and BEFORE `_claim_side_effect`: if `session_send_cap` is set, count successful steps with the design's SQL (explicit `r.tenant_id=$1`, `s.session_id=$3`). At or above the cap, or with an empty `session_id`, raise `_StepFailure("unavailable","send_cap_reached")` with no claim and no upstream request. It must be `unavailable`, not `invalid_argument`.
    - (h) After `_mark_side_effect_success`, if `claim_release_target(api_row)` is set, call `_release_booking_claim` inside a `try` that wraps ONLY that call. On failure log only `booking_claim_release_failed` with `custom_api_id`, never the event id or exception text.
      - `_release_booking_claim` uses the `DELETE … USING custom_apis b` statement with BOTH `c.tenant_id=$1` AND `b.tenant_id=$1`, `c.status='success'`, and `uuid.UUID(hex=event_id)` parsed before any SQL.
    - (i/j) The (f) call passes `upstream_responses` built from the post-transform `prior_responses`.
  - `api_exec_executor.py`: the `chain_status == "confirmation_required"` branch is the FIRST branch, setting `INVALID_ARGUMENT` and `payload={"awaiting_caller_confirmation": True}`. Add NO `_STATUS_MAP` entry.
  - `policy_resolver.py`: LEFT JOIN `oauth_connections oc ON oc.id=ca.oauth_connection_id AND oc.tenant_id=ca.tenant_id AND oc.deleted_at IS NULL`, plus `AND (ca.oauth_connection_id IS NULL OR oc.status='connected')`.
  - Done when design tests 7 and 19 (cap part) and 1(d) pass:
    - The first call makes zero upstream requests, and the same `turn_id` again still makes zero.
    - A new `turn_id` with identical arguments makes exactly one request. Changed arguments or an expired pending row re-prompt.
    - A cross-tenant pending row does not match (red with the predicate deleted).
    - A payload with `missing_fields` still returns exactly `{"awaiting_caller_confirmation": True}`.
    - Four sends in one session with four different hashes: the mock sees exactly 3, and the fourth takes no claim row.
    - A different session succeeds, and setting the cap to NULL lets the fourth through.
    - Cancel then rebook works, and a failed cancel (404 or timeout) releases nothing.
    - Cross-tenant cancel releases nothing (red with predicates deleted), and a release error still reports the cancel as a success.
    - A disconnected connection's APIs are absent from the policy resolver, and another tenant's connected row does not re-enable them.

- [ ] T24 Add preset apply and remove and mount them — `services/toolexec/presets.py` (apply and remove part), `services/toolexec/routers/connector_presets.py` (new), `services/toolexec/app.py`, tests. Requires T17, T21, T22, T23 (nothing gated can be applied before the gate, the cap and the caller-id plumbing exist).
  - `apply_preset`:
    - Pre-transaction: `resolve_and_validate_endpoint` on every step. For OAuth presets, `get_connected(conn, tenant_id, provider)` with an explicit predicate (`PresetConnectorRequired`, which becomes 409 `connector_required` or `connector_scope_required`).
    - WhatsApp: seal `key_ref` via `encrypt_tenant_secret(tenant_id, …)` (for Interakt the plaintext is `"Basic "+key`), then run `_validate_credential_ref`.
    - Sheets: create the spreadsheet and append the header row with `valueInputOption=RAW`, BEFORE the transaction.
    - Then `tenant_conn` plus `transaction()` plus `pg_advisory_xact_lock(hashtext('custom_apis:'||tenant_id))`. Insert missing steps by name via `_insert_custom_api`, recompute the chain levels once, and map `UniqueViolationError` to `preset_name_conflict`.
    - Re-applying a complete preset is a no-op.
  - `remove_preset` under the same lock: `DependentApiExists` if a row outside the preset has an upstream param into it. Otherwise soft-delete and write one audit row per id.
  - Routes: `GET /connector-presets`, apply (201, returns `public_custom_api` rows) and remove (204). Both writes use `require_role` and an awaited `assert_tenant_access`. Mount in `app.py` along with the `PresetConnectorRequired` handler.
  - Done when design tests 4 and 6 and 12 (WhatsApp attack) and 16 pass:
    - Apply, disconnect, reconnect and re-apply yields the same connection id and the same 5 row ids.
    - A denied-address endpoint or a Sheets failure creates no rows.
    - Remove with a hand-made dependent row gives 409.
    - A viewer's `GET` of the applied rows shows `key_ref=="[stored]"`.
    - A WhatsApp ref copied to B fails at write time AND at call time with zero attacker-host requests.
    - The Sheets append uses `RAW` and the body is unchanged.

- [ ] T25 Build the integrations UI — `admin-ui/components/ConnectorsPanel.tsx` (new), `admin-ui/app/(console)/integrations/page.tsx` (new), `admin-ui/app/(console)/integrations/callback/page.tsx` (new). Requires T17, T24, T11 (`toolexecApi.ts` gains the provider, connection, authorize, callback, disconnect and preset functions here or in T11). Read `admin-ui/AGENTS.md` and `node_modules/next/dist/docs/` first.
  - Connector cards: Connect, Reconnect and Disconnect, with status and account label. The Disconnect UI message is generic, and tells the admin to also remove Yuviz in the provider account, since the response is now `{"disconnected": true}` (T16).
  - Preset cards: Apply with defaults prefilled, and timezone from `Intl.DateTimeFormat().resolvedOptions().timeZone`.
  - The callback page reads `code`, `state`, `error` and `accounts-server`, calls `history.replaceState` IMMEDIATELY to strip them, reads `{tenantId, provider, returnTo}` from `sessionStorage`, POSTs the callback, and shows a fixed generic message on failure. It sets `<meta name="referrer" content="no-referrer">`.
  - The path is `/integrations/callback` (registered with five providers, so do not rename it). If the integrations page already exists from the one-click-crm-integrations feature, extend it rather than overwrite (none exists as of this plan).
  - `CustomApisPanel.tsx` (needs a tiny edit; the T11 owner leaves a hook): "Managed by preset" badge with Edit hidden, and a "Disabled — reconnect {provider}" badge. Count it as a fourth file if needed.
  - Done when, in a real browser (lesson 23), a Google test account runs Connect, Apply, Attach, a real-phone booking with a read-back and a yes, a cancel, Disconnect and Reconnect, and the callback URL is stripped after load. Record the click count and the elapsed time against design test 11.

## Phase 6: Runbook and join

- [ ] T26 Document the runbook, then verify the merged result — `docs/setup.md`. Requires all above.
  - Document `TOOLEXEC_OAUTH_REDIRECT_URI` (console origin plus `/integrations/callback`) and `TOOLEXEC_OAUTH_{GOOGLE,ZOHO,MICROSOFT}_CLIENT_ID` / `_CLIENT_SECRET_REF`, with the per-provider registration steps (Zoho multi-DC).
  - Document the release step `python -m services.toolexec.reencrypt_tenant_refs --dry-run`, then the real run with `--report <path>`, straight after the toolexec rollout. Include the T13 ceiling flags and defaults, and the exit code 2 meaning.
  - Say that `env:`/`k8s:` refs are platform-operator-only input and a tenant admin enters the plaintext key, and what a quarantined credential means for a tenant.
  - Run the FULL suite once on the combined tree (lesson 41). Re-apply `schema.sql` twice against the seeded DB.
  - Done when:
    - the full suite is green;
    - `pg_constraint.convalidated=true` for `custom_apis_auth_config_enc_bound` after the script;
    - the call-site tripwires from T3/T7 and the route-count tripwires are green;
    - the seed script runs on a fresh DB.
