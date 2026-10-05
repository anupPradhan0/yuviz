# Security review: branch `feature/connector-presets-oauth` (`git diff 4918800..HEAD`), code mode

VERDICT: GREEN

Round-5 (design) controls were re-verified by reading the enforcing code, not the design prose.
Two of round 5's "verified" claims were false (the `quarantined` reader, the `body_path`
redaction); both are now genuinely enforced at the lines named below. No critical or high is open.

## Findings

1. [low] `audit_log` is an un-inventoried sink for credential ciphertext — `services/config/audit.py:33-36`
   (`_redact` is a single-level dict comprehension; `auth_config` is not in `_SECRET_REF_FIELDS`)
   reached from `services/toolexec/presets.py:684-687` (`new_value=row`) and `:712-716`
   (`old_value=row`), where `row` comes from `_preset_rows` (`presets.py:605-619`) and carries
   `auth_config = {"token_ref": "enc:t1...."}` for `whatsapp_confirmation`.
   Attack: a console principal in tenant A that the `GET /audit-log` route admits reads A's own
   audit log and recovers the full credential ciphertext for A's WhatsApp key. It is tenant-bound,
   so `decrypt_tenant_secret`'s AAD (`libs/config_sdk/secrets.py:134`) blocks replay into another
   tenant and the blob is useless outside A — which is why this is low, not critical. The sharper
   half is completeness: `reencrypt_tenant_refs._scan` (`services/toolexec/reencrypt_tenant_refs.py:112-162`)
   inventories only `custom_apis` + the four Config ref columns, so any **legacy** unbound `enc:`
   ref that a past `create_custom_api`/`update_custom_api` audit write copied into
   `audit_log.new_value` survives the quarantine run untouched, and `VALIDATE CONSTRAINT` then
   reports the cutover complete. A legacy ref opens for whoever holds it (lesson 43), so that
   residue is a bearer capability sitting outside the control that was built to retire it.
   Fix: add `"auth_config"` to `_SECRET_REF_FIELDS` and make `_redact` recurse into nested dicts
   redacting any key ending `_ref`; separately, either add `audit_log` to the quarantine scan or
   state in the script's docstring that pre-change audit rows are out of scope and why.

No other finding met the bar (an actor, their starting access, and what they end up with).

Scope caveats on controls that are otherwise sound, recorded so the next reader does not re-derive
them — not findings, because I could not write an attacker sentence for either:
- `_check_ceiling`'s `platform_rows` tripwire (`reencrypt_tenant_refs.py:206`) looks for
  `tenant_id IS NULL` on `provider_configs` only; `carriers`, `tool_provider_configs` and
  `telephony_configs` can also hold platform rows, and quarantining one would not trip the ceiling.
  Operator-visible immediately, and no tenant principal can influence a platform row's ciphertext.
- `oauth.complete_authorization` does not verify the `id_token` signature
  (`services/toolexec/oauth.py:152-156`). Permitted by OIDC Core 3.1.3.7 because the token arrives
  on the TLS channel direct from the provider's token endpoint, and the claims are used only for
  `account_label` (display) and `provider_sub` (the shared-grant probe) — never as an auth input.

## Verified controls

- `resolve_api_key_input`'s `allow_pointer_schemes` is keyword-only and defaultless
  (`services/config/provider_configs.py:58-64`); all eight router call sites pass the literal
  `is_platform_scoped(current_user)` — `routers/carriers.py:57,94`, `routers/telephony_configs.py:80,139`,
  `routers/provider_configs.py:81,143`, `routers/tool_provider_configs.py:65,113`. No variable, no
  role check (lesson 24 respected).
- The call-site tripwire is genuinely derived, not hand-listed: `tests/call_sites.py:33-41`
  `functions_taking()` reads the *signatures* under `services/config/`, and
  `test_credential_masking.py:267-272` asserts the callee count (12), that every call passes the
  keyword, and the total call-site count (92) — so a new callee or a new call site goes red
  (lesson 12's "assert the enumeration's own size"). `call_sites.py:23` skips dot-prefixed dirs, so
  `.claude/worktrees/` copies do not pollute the count. A second, narrower tripwire at
  `test_provider_secret_input.py:114-120` pins 50 sites and explicitly asserts
  `scripts/seed_default_config.py` is among them.
- `current_ref=` is taken from a row read under `SELECT … FOR UPDATE` in the same transaction on all
  four ref tables: `provider_configs.py:247→259`, `carriers.py:125→136`,
  `tool_provider_configs.py:135→145`, `telephony_configs.py:331` (`_normalize_one` compares against
  the locked row). Responses mask through `mask_enc`/`public_provider_config`
  (`provider_configs.py:44-55`) and `public_custom_api` (`custom_apis.py:218-227`), which is now
  applied on all four toolexec custom-API routes (list, create, get, patch).
- `_mask_ref`/`mask_enc` render the `quarantined` sentinel as `""`, not as the literal, so the panel
  re-asks rather than storing the sentinel back.
- Quarantine fail-closed at the telephony reader: `services/telephony/accounts.py:133-142` now
  `is_quarantined(entry) → return None` **before** the non-`enc:` pass-through. The sentinel is
  single-sourced at `libs/config_sdk/secrets.py:30` (lesson 42). A tenant-bound `enc:t1.` ref
  reaching this shared reader also fails closed: `is_encrypted` is true, `decrypt_secret` refuses it
  at `secrets.py:86-88`, and the `except` skips the row.
- No unbound-decrypt fallback anywhere: `auth_schemes.resolve_tenant_ref` (`auth_schemes.py:139-151`)
  rejects any `enc:` that is not `enc:t1.` before touching a resolver, and
  `secrets.decrypt_secret` itself refuses tenant-bound input. `validate_tenant_ref`
  (`auth_schemes.py:67-85`) accepts *only* an `enc:t1.` that opens for the row's own tenant — a
  legacy Fernet token and `env:`/`k8s:` are refused on the write side (lesson 37 closed).
  Schema CHECKs back it: `oauth_connections.*_ref LIKE 'enc:t1.%'`,
  `oauth_authorization_states.code_verifier_ref LIKE 'enc:t1.%'`, and the NOT VALID
  `custom_apis_auth_config_enc_bound` that the script validates at the end of its transaction.
- Quarantine script (`reencrypt_tenant_refs.py`): `LOCK TABLE … SHARE ROW EXCLUSIVE` on all five
  tables first (`:349-351`); every write is conditional on the exact old value —
  `_replace_scalar:254` (`WHERE id = $1 AND <col> = $2`), `_replace_credentials:266`,
  `_replace_custom_api:240` (`WHERE id = $1 AND tenant_id = $2 AND auth_config = $3::jsonb`) — and
  `_executed_one` raises on zero rows, aborting the single enclosing transaction (`:235-237`).
  The ceiling runs **before** `_apply` (`:355`) and exits 2 with counts only. The report is written
  0600 via `os.open(..., 0o600)` + `fchmod` and only `if args.report and not args.dry_run`, i.e.
  after `asyncio.run` returned from a committed transaction (`:381-387`, `:407-408`). Exit 1 unless
  `remaining == 0 and config_shared == 0` (`:417-418`), and exit 1 with the exception **type** only
  so an asyncpg CheckViolation detail cannot print a row.
- The `custom_apis` whole-document deviation is safe: `old_doc` is the same row's
  `auth_config::text` read inside the locked transaction, re-cast to `jsonb` in the predicate, so
  the comparison is canonical-jsonb equality against exactly the document that was scanned. All of
  a row's rebinds and quarantines are merged into one `custom_api_fields[row_id]` dict and written
  once (`:294-312`), which is what makes a two-legacy-field row convert atomically — the per-field
  predicate the design specified would have left the row half-converted and tripped the NOT VALID
  CHECK that is re-evaluated on every UPDATE. Zero rows still aborts, so the deviation does not
  weaken the loser's path.
- OAuth state: stored as sha256 hex only (`oauth.py:183`), tenant- and user-bound, 10-minute TTL.
  Redemption is one conditional `UPDATE … SET consumed_at = now() WHERE tenant_id = $1 AND
  state_hash = $2 AND user_id = $3 AND consumed_at IS NULL AND expires_at > now() RETURNING …`
  (`oauth.py:205-212`) — single-use enforced atomically, not check-then-act (lesson 8), and the
  `tenant_id` predicate is present. The PKCE verifier is stored as `encrypt_tenant_secret(tenant_id, …)`
  and read back through `resolve_tenant_ref`. Zoho's `accounts_server` is allow-listed against the
  provider spec before any URL is formatted. No route takes a tenant identifier from a body, query
  or header: every one is `/tenants/{tenant_id}/…` behind `bind_path_tenant` +
  `require_path_tenant_access`, with an awaited `assert_tenant_access` in each handler (lesson 38)
  and `require_role("superadmin","admin")` on all three writes.
- `GET /oauth-providers` (`routers/oauth_connections.py:39-41`) returns only platform-level
  `{key,label}` for providers whose env is configured — identical for every authenticated caller,
  nothing tenant-derived. Same for `GET /connector-presets` (static catalogue + JSON schema).
- `disconnect` (`oauth.py:398-432`): foreground work is one conditional UPDATE whose inner
  `SELECT … WHERE tenant_id = $1 AND id = $2 … FOR UPDATE` and outer `c.tenant_id = $1` both carry
  the tenant, plus the audit row; the body is the constant `{"disconnected": True}`. The
  shared-grant probe and the revoke run in a Starlette `BackgroundTasks` callback that executes
  after the response is sent, so neither body nor latency varies with another tenant's state
  (lesson 2). `_revoke_upstream` wraps everything in `try/except Exception` and logs
  `connection_id` only — no provider body, no token, no other tenant's identifier — so the callback
  cannot leak through its error path either. The `EXISTS` probe takes `provider_sub` from the
  disconnecting row's own `RETURNING o.provider_sub` (`:418`), never from the caller; the
  `provider_sub` column is excluded from `_PUBLIC_COLUMNS` (`oauth.py:52`) so no route returns it.
- Connector tokens cannot be redirected: `auth_schemes.apply`'s `oauth2_authorization_code` branch
  (`auth_schemes.py:228-236`) refuses unless `urlsplit(endpoint_url).hostname` is in that provider's
  `api_hosts`; `oauth.post_json` applies the same check. `access_token_for` and `_winners_token`
  read `oauth_connections` with `tenant_id = $1 AND id = $2`, and the refresh/flip writes are
  conditional `UPDATE … WHERE tenant_id = $1 AND id = $2 AND refresh_token_ref = $3 AND status =
  'connected'` with a loser's path (lesson 8). `custom_apis.oauth_connection_id` is fenced by the
  composite FK `(oauth_connection_id, tenant_id) REFERENCES oauth_connections(id, tenant_id)`
  (lesson 36's related note), so a tenant cannot point a row at another tenant's connection.
- Tenant-authored custom APIs cannot reach the new primitives at all: `CustomApiCreate`/`Update`
  restrict `auth_scheme` to `none|api_key|bearer|oauth2_client_credentials`
  (`schemas.py:44,61`) and `source` to `literal|caller|upstream` (`schemas.py:26`), and none of
  `oauth_connection_id`, `preset_key`, `response_transform`, `confirmation_template`,
  `session_send_cap`, `idempotency_body_field` is exposed — they are written only from
  `presets.py` through `_insert_custom_api`. Schema CHECKs pin `value_prefix` and
  `value_digits_only` to `source = 'caller_id'`.
- `caller_id` is server metadata only. `_resolve_arguments` (`executor.py:459-465`) reads
  `remote_party` and never `caller_arguments` for a `caller_id` param, and fails closed
  (`_StepFailure("invalid_argument","caller_id_unavailable")`) when it is `None`.
  `presets.remote_party_number` (`presets.py:332-347`) branches on direction — caller on inbound,
  callee on outbound — and returns `None` for any other direction, an unusable number, or two legs
  that normalize equal (lessons 39/44). `_remote_party` (`executor.py:93-109`) adds the belt: a
  number matching one of this tenant's own `phone_numbers` rows is refused, which is what an
  outbound call mislabelled `inbound` would present. The wiring is server-set end to end —
  `__main__.py:378-381` is the only construction site and passes `ctx.direction`/`ctx.called_did`;
  `pipeline.py:449-452` dropped the `"inbound"` default so an omitted direction fails closed.
  `policy_resolver.py:261` joins params with `AND p.source = 'caller'`, so a `caller_id` param never
  appears in the model's tool schema — the model cannot name it, let alone set it.
- `body_path` redaction is real: `_redaction_keys` (`executor.py:558-569`) emits a `$.a.b` JSONPath
  for a sensitive param that sits at a `body_path` and the bare name otherwise, and
  `redaction._redact_one` (`redaction.py:36-64`) walks that subset into the nested outbound body.
  All six preset `caller_id` params are declared `sensitive=True` (`presets.py:118-124`).
  `_interpolate_success_template` refuses to speak the `[redacted]` sentinel back (`executor.py:804-806`).
- Send cap (`executor.py:997-1002`, `_sends_in_session:718-725`): `r.tenant_id = $1` predicate
  present, counts `s.status = 'success'` only, and an empty `request.session_id` short-circuits to
  the raise via `not request.session_id or …` — fails closed. `session_id` comes from the
  Conversation service's call session, not from the model or a tenant body field, so it cannot be
  rotated inside one call to reset the count. The failure is `unavailable`, not `invalid_argument`,
  so the model is not invited to retry with a different recipient.
- RLS: `oauth_connections` and `oauth_authorization_states` both get a FOR ALL policy on
  `yuviz_app` with matching `USING`/`WITH CHECK` on `app.tenant_id`, plus `ENABLE` and `FORCE`
  (`database/rls.sql:251-271`), and `oauth_connection` is added to the audit-log tenant backfill.
  Nothing in the new code runs as a bypass role except the one deliberate `platform_conn(pool,
  reason="oauth-shared-grant-check")` in `_revoke_upstream`, whose result is a boolean that never
  leaves the process. Per lesson 36 the app still connects as the superuser DSN, so every one of
  these reads *also* carries an explicit `tenant_id = $1` predicate — I checked each statement in
  `oauth.py`, `presets.py` and `executor.py` individually; there is no RLS-only read.
- Preset apply/remove: both take `pg_advisory_xact_lock(hashtext('custom_apis:' || tenant_id))`
  inside the transaction (`presets.py:674`, `:697`), so the lock namespace is per-tenant and one
  tenant cannot serialize another. Every statement in both carries `tenant_id = $1`. Apply does all
  fallible work (connector lookup, scope subset check, endpoint SSRF validation, credential sealing,
  Sheets setup call) *before* opening the write transaction, so a failure leaves no rows. Scopes for
  an authorize come only from `presets.PRESETS[...].scopes`, never tenant input (`oauth.py:165-172`).
  Remove's dependency probe is scoped `ca.tenant_id = $1`.
- `value_digits_only` only strips a leading `+` from a value that is already
  `"+" + 8..15 digits` produced by `normalize_ani` — it cannot widen what a `caller_id` param
  carries, and the schema CHECK confines it to `source = 'caller_id'`.
- SSRF/DNS-rebinding on the new outbound paths: `_provider_call` (`oauth.py:135-142`) is the only
  egress in `oauth.py`, goes through `resolve_and_validate_endpoint` + `PinnedResolverTransport`
  (`custom_apis.py:150-172`, which pins the validated IP while keeping SNI and the Host header on
  the real hostname), has a 10s timeout, and has **no** `params=` — secrets travel only in `data=`
  (lesson 20). `__main__.configure_logging` quiets `httpx`/`httpcore` to WARNING, and
  `scripts/start_local.sh:134` launches toolexec as `python3 -m services.toolexec`, so that function
  actually runs in the deployed path; `tests/test_logging_config.py` asserts zero httpx/httpcore
  records for a URL carrying a path secret.
- Error shapes are constant and content-free: the OAuth callback route maps every `ValueError` to a
  fixed `400 oauth_connection_failed`; `app.py`'s `LookupError` handler returns a fixed detail
  rather than `str(exc)`; `complete_authorization` collapses every exchange failure into one code so
  the provider's response body never reaches a client or a log.

---

## Correction to the [low] `audit_log` finding — NOT A FINDING

Checked before fixing, and it does not hold. The finding traces
`services/toolexec/presets.py:684-687,712-716` to `services/config/audit.py:33-36`,
whose `_redact` does not list `auth_config`. But `presets.py:29` imports
`audit` from `services.toolexec`, not from `services.config`, and
`services/toolexec/audit.py:34` has `_SECRET_FIELDS = {"auth_config"}` and
redacts on `k.endswith("_ref")` as a second net. The preset writes are
masked wholesale.

The sharper half — a legacy unbound `enc:` ref copied into `audit_log` by a
past custom-API write surviving the quarantine run — rests on the same
mistake. `git show 4918800:services/toolexec/audit.py` is byte-identical in
that block, so the masking is not something this branch introduced and there
is no window in which a custom-API `auth_config` ciphertext reached
`audit_log` through it.

What would make it real, and is worth a later look: the two `_redact`
implementations have diverged (Config's is a name list without `auth_config`;
toolexec's masks the container and matches the `_ref` suffix). Neither is
reachable with a credential today, but a service that starts writing a nested
credential through the Config one would not be caught by either reviewer,
because each reads only the module in front of it.
