# Security review: 02-design.md (round 3, final)
VERDICT: GREEN

Round-2 status: finding 1 (high) **closed** — the mechanism was replaced, not patched, and the
replacement holds against the real wiring; findings 2, 3, 4, 5, 6 (medium) **closed**; finding 7
(low) **closed**. One new low is opened below, on the scope of finding 1's own guard-rail test.

## Findings

1. [low] The "exactly two routes depend on `get_authenticated_user`" regression test is specified
   against the Config app only, but three other FastAPI apps import the same dependency module —
   02-design.md "Risks" (first bullet) + "Test plan / Console gate"
   Attack: `services/knowledge/routers/*.py`, `services/did/routers/numbers.py` and
   `services/campaigns/routers/campaigns.py` all do `from services.config.deps import
   get_current_user, require_role` — so they inherit the console gate correctly today, but they are
   also able to import `get_authenticated_user`. A maintainer adding a Knowledge or DID route next
   quarter who reaches for `get_authenticated_user` (its name reads like the more basic, more
   obvious choice) hands any `supervisor`/`agent` JWT — which `POST /auth/login` still issues
   happily — read access to knowledge-base documents or purchased-DID inventory, and the design's
   own mitigation test, which enumerates `services/config.app.routes`, passes clean. The gate is
   sound; its tripwire is one app narrower than the gate's blast radius.
   Fix: make the test import each service's `app` (`services.config.app`, `services.knowledge.app`,
   `services.did.app`, `services.campaigns.app`) and assert `get_authenticated_user` appears in
   exactly the two Config `/auth` routes across all four; or move `get_authenticated_user` behind a
   leading underscore in `deps.py` and expose it only to `routers/auth.py`.

## Verified controls

**Finding 1 — the console-role gate. Closed; the new shape is exempt-by-construction as claimed.**
- The transitive claim holds against the real code: `deps.require_role()`'s inner `_check` is
  literally `async def _check(user: CurrentUser = Depends(get_current_user))` (`deps.py:43`), so
  every `require_role(...)` route resolves through `get_current_user` and inherits the gate. There
  is no second identity path: `decode_access_token` is referenced in exactly two places in the whole
  repo — its definition in `auth.py:59` and its single call site in `deps.py:32`. No router, no
  middleware and no other service decodes a JWT itself or reads the `Authorization` header for
  inbound auth (the only other `Bearer` producers are outbound clients: `libs/config_sdk`,
  `libs/knowledge_sdk`, `services/vobiz/app.py:79`). A router-level `dependencies=` therefore cannot
  bypass the gate, because the gate is not a dependency of the route — it is part of resolving the
  identity the route asked for.
- Unauthenticated routes genuinely never reach it: `/health` is `@app.get` on the app object with no
  dependency (`app.py:112`), and `/auth/setup-status`, `/auth/bootstrap`, `/auth/login` take no
  identity dependency at all (`routers/auth.py:22, 27, 39`). The docker-compose healthcheck that
  gates `conversation` and `knowledge` on `service_healthy` is unaffected. The two new public
  accept routes are new code and take no identity dependency by design. This is the failure mode
  lesson 1 was earned on, and the new shape does not have it.
- `/auth/me` (`routers/auth.py:51`) and `/auth/change-password` (`routers/auth.py:63`) are, by
  enumeration, the *only* two routes that must move: they are the only `get_current_user` call sites
  in `routers/auth.py`, and every other `get_current_user`/`require_role` route in the repo is a
  configuration-console route that a `supervisor`/`agent` is intended to be 403'd from. No route
  that should have moved was missed, so there is no functional break to report. A `supervisor` can
  still log in, read `/auth/me` and rotate their own password; everything else 403s, which is the
  intent.
- The gate's reach is *wider* than the design states, in the safe direction: `services/knowledge`,
  `services/did` and `services/campaigns` import `get_current_user` from `services.config.deps`, so
  they are gated too. Verified this locks nothing out — every role those services use today
  (`superadmin`, `admin`, `viewer`) is inside `CONSOLE_ROLES`. (The design should say so; see
  finding 1 for the one consequence that matters.)
- `CONSOLE_ROLES` fails **closed**, confirmed: the check is `if user.role not in CONSOLE_ROLES:
  403` — an allowlist, so a role added to `users_role_check` and forgotten here is denied
  everywhere, never admitted. The design's concession ("fails closed but silently") is accurate,
  and the mitigation test that forces every role in the CHECK constraint to be explicitly classified
  is the right shape.
- Service accounts are not locked out: `scripts/create_service_account.py:33` is
  `users.create_user(..., role="viewer", tenant_id=None)` — `viewer` is in `CONSOLE_ROLES`. Grepped
  every role literal assignment in the repo: the only two are that line and
  `scripts/create_superadmin.py:36` (`superadmin`); `users.py:145` hard-codes `superadmin` for
  bootstrap. No service account is ever created with any other role, and `users.create_user`'s
  `role` default is `admin`, also in `CONSOLE_ROLES`. Conversation and Knowledge keep their Config
  API access.
- No role comparison anywhere grants write access by *negation* (`role != "viewer"`-style), which
  would have handed the new roles a write path around the gate: grepped every `.role` comparison in
  `services/` and `libs/` — the only authorization comparisons are `deps.py:44`'s allowlist and
  `routers/users.py:28-31`'s `!= "superadmin"` narrowing, which sits *inside*
  `require_role("superadmin","admin")` and so is unreachable by a supervisor/agent.

**Finding 2 — the account-existence oracle. Closed.**
- Two counters keyed on the JWT subject, the 30/hour probe cap checked *before* the users lookup and
  counting 201/409/403/429 alike, makes a conflict cost exactly what a success costs — the
  asymmetry that made the round-2 oracle free is gone. Checked for a cheaper probe elsewhere and
  found none: `POST /auth/login` returns one 401 for unknown-email and wrong-password
  (`users.authenticate` docstring, `routers/auth.py:44`); `POST /auth/bootstrap` returns
  "setup has already been completed" once a superadmin exists, so its "already registered" 409 is
  unreachable post-setup; `GET /users` is tenant-scoped and excludes service accounts and
  superadmins for a tenant actor (`users.py:58-61`); `POST /users`, the other create path, is
  deleted by this design. The residual one-bit-per-attempt leak at 30/hour is inherent to the PRD's
  globally-unique-email decision and is stated as accepted.

**Finding 3 — accept-path unique violation. Closed.**
- Confirmed the gap was real: `app.py` registers handlers only for `LookupError`,
  `SecretEncryptionUnavailable`, `ValueError` and `ForeignKeyViolationError` — an unhandled
  `UniqueViolationError` would have been a raw 500. The design now catches it in `accept_invite` and
  raises `EmailTaken` → `409 "an account already exists for this email"`, tenant-blind for an
  unauthenticated caller (lesson 2). Because `EmailTaken` propagates *out* of the `conn.transaction()`
  block rather than being swallowed inside it, the transaction genuinely rolls back — asyncpg would
  reject further statements on an aborted transaction otherwise — so the invite stays `pending`,
  resendable and revocable. That is the specified behaviour and it is implementable as written.

**Finding 4 — soft-delete re-invite. Closed.**
- `users_email_lower_idx ... WHERE deleted_at IS NULL` now agrees exactly with `get_user_by_email`'s
  predicate (`users.py:33`, `WHERE email = $1 AND deleted_at IS NULL`), so the pre-check and the
  constraint see the same row set: a re-invited departed employee no longer passes the check and
  then 500s on the insert. Dropping the case-sensitive `users_email_key` (the auto-generated name
  for `email TEXT NOT NULL UNIQUE`, `schema.sql:223`) is what makes the ghost row harmless, and the
  design correctly calls out and accepts that two soft-deleted rows may now share an address —
  unreachable, since `get_user_by_email`, `authenticate` and every listing filter `deleted_at IS NULL`.

**Finding 5 — the backfill. Closed.**
- The `DO $$` block counts live case-insensitive duplicates and `RAISE EXCEPTION`s with a named
  remedy *before* the UPDATE, satisfying lesson 5. The ordering is right and load-bearing:
  `users_email_key` is dropped first, so the UPDATE has no case-sensitive constraint to abort on,
  and `users_email_lower_idx` is created last, so it cannot fire mid-backfill. Checked the
  soft-deleted edge: the guard only counts live rows but the UPDATE touches all rows — safe,
  because after the DROP there is no constraint covering deleted rows and the new index is partial.
  Re-running `schema.sql` is idempotent: emails are already lower-cased, so the UPDATE matches zero
  rows and the guard finds zero collisions.

**Finding 6 — accept ordering. Closed; the loser really gets 410.**
- The conditional UPDATE is now step 2 with `accepted_user_id` deliberately unset, so the invite row
  is the first lock taken. Traced the race under READ COMMITTED: both accepts pass the classifying
  SELECT, the second blocks on the row lock at
  `UPDATE ... WHERE id=$1 AND status='pending' AND expires_at > now()`, and on unblocking Postgres
  re-evaluates the qual against the committed new row version (`status='accepted'`) — zero rows,
  `InviteUsed`, 410 "already used". It never reaches step 3, so it can never serialise on
  `users_email_lower_idx` and can never produce the 500 of round 2. `accepted_user_id` is backfilled
  in step 4 inside the same transaction, so the FK is populated atomically. This is exactly the
  shape lesson 8 prescribes, including the column-ordering caveat.

**Finding 7 — throttle key. Closed.**
- Pinned to `request.client.host` with `X-Forwarded-For` explicitly not read, plus a concrete
  migration note (trusted-proxy list, right-most untrusted hop) and a test asserting a forged
  `X-Forwarded-For` lands in the same bucket. Correct for a deployment with no reverse proxy in
  front of the Config Service.

**Still-sound controls carried forward from rounds 1-2 (re-checked, unchanged):**
- Tenant-blind 409 on invite create for a tenant_admin, byte-identical across own-tenant user,
  own-tenant pending invite and other-tenant user; only super_admin sees the slug, and the PRD's AC9
  was amended in parallel so design and PRD agree.
- `may_invite` is the single choke point for create/resend/revoke, taking actor role and tenant only
  from the decoded JWT, with resend/revoke passing the *stored* row's role/tenant so an enumerated
  cross-tenant invite id is refused by the same predicate; `supervisor`/`agent` denied as actors.
- `?tenant_id=` on `GET /invites` and `GET /users` is honoured only for super_admin and forced to
  the JWT tenant otherwise — client-supplied tenancy is never trusted.
- 256-bit `secrets.token_urlsafe(32)` stored and compared only as a sha256 digest against a UNIQUE
  column, redacted from `audit_log`, never logged or echoed; delivered in a URL *fragment* and
  forwarded in an `X-Invite-Token` header, so it never reaches an access log or a `Referer`.
- Per-tenant pending-invite unique index (`COALESCE(tenant_id, zero-uuid), lower(email) WHERE
  status='pending'`) — no cross-tenant squatting lock (lesson 3).
- Email normalisation is closed end-to-end: `_insert_user` lower-cases, `update_user`'s
  `_UPDATABLE_FIELDS` is `{role, tenant_id}` so email cannot be mutated back to mixed case, and
  every insert path (`create_user`, `bootstrap_first_superadmin`, invite accept) funnels through
  `_insert_user`. There is no way to reintroduce a mixed-case address after the backfill.
- Role escalation via `PATCH /users/{id}` stays superadmin-only (`routers/users.py:49`), so widening
  `UserUpdate`'s role `Literal` to include `supervisor`/`agent` grants a tenant_admin nothing.
- `to_public_dict` strips `password_hash` on every user-shaped response, including the login body.
- Round-1 findings 9 and 10 remain explicitly deferred in Risks with stated reasoning, not dropped;
  11 is fixed.

**Note for the implementer (not a security finding):** `services/config/auth.py:23` still declares
`Role = Literal["superadmin", "admin", "viewer"]` for `CurrentUser.role`, and the Changes table does
not list `auth.py`. It is a plain frozen dataclass with no runtime validation, so a
`supervisor`/`agent` token decodes fine and nothing breaks — but the annotation will be wrong and
should be widened alongside `users_role_check`.
