# Design: Invite-based user onboarding (Platform / Tenant)

## Approach
One new table, `user_invites`, holds the lifecycle and *is* the state machine: `pending/accepted/revoked` is a stored `status`, `expired` is derived (`status='pending' AND expires_at < now()`), and checking status before expiry is what lets AC10 answer "already used" distinctly from revoked and expired. Only `sha256(token)` is stored, like a password-reset token; the accept endpoints are **public** (no JWT) and look up by `token_hash`, never by id or email. AC2/AC3/AC4 collapse into one pure function, `invites.may_invite(...)`, which **every** mutating admin path calls — resend and revoke load the row first and pass its *stored* `role`/`tenant_id`, so an enumerated `invite_id` from another tenant, or a super_admin's invite, is refused by the same rule that blocks creating one (no IDOR, no inline `if` chains like `create_user`'s today). SMTP send is **inline, after commit, non-fatal**: the row is already `pending`, so a failure returns `email_sent: false` (AC12) — no queue needed.

Two things the security review forced into the design rather than into the implementer's discretion. First, **nothing in an error response may describe another tenant**: a tenant_admin's conflict on AC9 is a flat "this email cannot be invited", pending-invite uniqueness is scoped *per tenant* so it cannot be used as a squatting lock, and the conflict itself is rate-limited so it is not a free existence oracle. Second, `supervisor` and `agent` are **runtime roles with no Config API surface at all** in this build — adding them to the role CHECK would otherwise hand them every route guarded only by `Depends(get_current_user)` (28 of them: transcripts, provider configs, carriers, telephony inventory, the user list). That gate is placed *inside* `get_current_user` itself, for the reasons traced below.

## The console-role gate (finding 1 — the mechanism, traced)
**Rejected: an app-level `dependencies=[Depends(require_console_role)]` on `FastAPI(...)`.** It cannot work here. `deps.get_current_user` raises 401 on a missing/malformed `Authorization` header *before* any exemption logic could run, so an app-level guard built on it breaks every unauthenticated route: `/health` (curled by `deployment/docker/docker-compose.yml:96`, with `conversation` and `knowledge` gated on `condition: service_healthy` — this fails at deploy, not at review), `/auth/setup-status`, `/auth/bootstrap`, `/auth/login`, and the two public invite-accept routes. Making auth optional inside the guard "fixes" that by creating an unauthenticated passthrough on all 28 routes. An app-level dependency is also not reliably a ceiling: `app.include_router(r)` composes with whatever `r` was constructed with, so a router carrying its own `dependencies=` still runs the app-level one — but the reverse mistake (assuming a router-level list *replaces* it) is exactly the kind of thing that gets edited wrong later. A per-router allowlist has the opposite failure: 13 router files today, and the fourteenth router someone adds next quarter silently omits it.

**Chosen: put the check inside `deps.get_current_user`, and give the two self-service auth routes a separate, unchecked dependency.** Every authenticated route in this service reaches identity through exactly one function — `get_current_user`, either directly (28 routes) or transitively, since `require_role()`'s inner `_check` is itself `Depends(get_current_user)`. Unauthenticated routes never call it, so `/health`, `/auth/login`, `/auth/bootstrap`, `/auth/setup-status` and the public accept pair are untouched by construction rather than by an exemption list that has to be maintained. A router's own `dependencies=` cannot bypass it, because the check is not a dependency at all — it is part of resolving identity.

```python
# deps.py — CONSOLE_ROLES is an allowlist, not a denylist of the two new roles.
CONSOLE_ROLES = frozenset({"superadmin", "admin", "viewer"})

async def get_authenticated_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    """Today's get_current_user body verbatim: decode or 401. No role gate."""

async def get_current_user(user: CurrentUser = Depends(get_authenticated_user)) -> CurrentUser:
    """Adds the console gate: 403 if user.role not in CONSOLE_ROLES.
    Every configuration route keeps depending on this name, so a new router
    written next year inherits the gate without its author knowing it exists."""
```

Only two call sites change: `routers/auth.py`'s `GET /auth/me` and `POST /auth/change-password` switch to `Depends(get_authenticated_user)`, because a supervisor or agent must still be able to log in, see who they are, and rotate their own password. Nothing else in `services/config/` moves.

**Service accounts are not locked out.** `scripts/create_service_account.py:33` creates them with `role="viewer"`, `tenant_id=None`, and `viewer` is in `CONSOLE_ROLES` — so Conversation and Knowledge keep their Config API access unchanged. The test plan asserts this explicitly rather than trusting the reading.

## Changes
| File | Change | Why |
|---|---|---|
| `database/schema.sql` | Widen `users_role_check`; add `users.team`; drop the case-sensitive `users_email_key` and replace it with a guarded lower-casing backfill + `WHERE deleted_at IS NULL` functional unique index; new `user_invites` table with a per-tenant pending-email index | Idempotent SQL, no Alembic — matches this file's existing ALTERs |
| `services/config/deps.py` | Split `get_authenticated_user` (raw) from `get_current_user` (console-gated); `CONSOLE_ROLES` | The one choke point every authenticated route already passes through |
| `services/config/routers/auth.py` | `/auth/me` and `/auth/change-password` depend on `get_authenticated_user` | The only two routes a supervisor/agent legitimately needs |
| `services/config/invites.py` (new) | `may_invite`, create/list/resend/revoke/accept, token generation, cooldown + probe limits, audit writes | Logic sits beside routers, not in them (CURSOR.md) |
| `services/config/email.py` (new) | `send_invite_email()`; password via `SMTP_PASSWORD_REF` secret ref | stdlib `smtplib`; no new dependency |
| `services/config/routers/invites.py` (new) | `require_role("superadmin","admin")` on admin routes; accept routes unauthenticated + throttled | Mirrors `routers/users.py` |
| `services/config/app.py` | `app.include_router(invites.router)`; in-process throttle helper | Existing wiring |
| `services/config/routers/users.py` | Delete `POST /users` (the temp-password path); `list_users` gains `?tenant_id=` | Invite is the only account-creation path |
| `services/config/users.py` | `_insert_user()` gains `team` and lower-cases `email`; `get_user_by_email` lower-cases its argument | Exists to insert+audit in a caller's transaction |
| `services/config/audit.py` | Add `token_hash` to the redaction set | Same treatment as `api_key_ref` |
| `services/config/schemas.py` | `InviteCreate`, `InviteAccept` (`password: str = Field(min_length=8)`); widen `UserUpdate`'s role `Literal` | Existing `Literal`-role convention; matches `ChangePasswordRequest` |
| `admin-ui/lib/api.ts` | `Invite`/`User` types + `listUsers`, `listInvites`, `createInvite`, `resendInvite`, `revokeInvite`, `getInvite`, `acceptInvite` | `request()` already attaches the JWT / handles 401 |
| `admin-ui/app/users/page.tsx` (new) | Users + pending-invites tables, invite modal, resend disabled during cooldown | Follows `app/tenants/page.tsx` (`refresh`, `Modal`, `ApiError.detail`) |
| `admin-ui/app/invite/page.tsx` (new) | Public set-password page; token read from the URL **fragment** (`/invite#<token>`) | Keeps the raw token out of logs/Referer |
| `admin-ui/components/AppShell.tsx` | Nav item (superadmin/admin only); treat `/invite` as standalone like `/login` | Else the guard bounces invitees to login |
| `services/config/tests/test_invites.py` (new), `test_users.py`, `test_auth.py` | Coverage below; drop `POST /users` cases | |
| `docs/setup.md` | `SMTP_HOST/PORT/USER/FROM`, `SMTP_PASSWORD_REF`, `INVITE_BASE_URL` | Where `JWT_SECRET` is documented |

## Data
```sql
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
  CHECK (role IN ('superadmin','admin','supervisor','agent','viewer'));
ALTER TABLE users ADD COLUMN IF NOT EXISTS team TEXT;

-- Email identity (findings 4, 5, 8). Three things happen here, in this order,
-- and the order is load-bearing.
--
-- (1) Drop the case-sensitive column UNIQUE first. If it survived, step (2)
--     could abort on it, and it would also keep a soft-deleted address
--     permanently un-reinvitable — the exact mismatch with
--     get_user_by_email()'s `deleted_at IS NULL` filter (users.py:33) that
--     would 500 the accept path when someone re-invites a departed employee.
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key;

-- (2) Lower-case, but refuse loudly instead of aborting mid-apply if two live
--     rows would collide (lesson 5). Collision is expected to be impossible
--     today — every row comes from bootstrap_first_superadmin(),
--     create_service_account.py or the invite flow, none of which have ever
--     written a mixed-case address — so this is a guard, not a migration.
--     The RAISE names the remedy rather than leaving a constraint error.
DO $$
DECLARE collisions int;
BEGIN
  SELECT count(*) INTO collisions FROM (
    SELECT lower(email) FROM users WHERE deleted_at IS NULL
    GROUP BY 1 HAVING count(*) > 1
  ) d;
  IF collisions > 0 THEN
    RAISE EXCEPTION
      'users.email has % case-insensitive duplicate(s) among live rows; '
      'soft-delete or merge the losers before applying schema.sql', collisions;
  END IF;
  UPDATE users SET email = lower(email) WHERE email <> lower(email);
END $$;

-- (3) Partial, so it agrees exactly with get_user_by_email()'s predicate: a
--     soft-deleted address is re-invitable and its ghost row cannot collide
--     with the new one.
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx
  ON users (lower(email)) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS user_invites (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID REFERENCES tenants(id),   -- NULL = superadmin invite
    email        TEXT NOT NULL,                 -- stored already lower-cased
    role         TEXT NOT NULL CHECK (role IN ('superadmin','admin','supervisor','agent','viewer')),
    team         TEXT,
    token_hash   TEXT NOT NULL UNIQUE,          -- sha256 hex of the token
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','revoked')),
    expires_at   TIMESTAMPTZ NOT NULL,
    invited_by   UUID REFERENCES users(id),
    accepted_at  TIMESTAMPTZ,
    accepted_user_id UUID REFERENCES users(id),
    last_sent_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-TENANT, not global (finding 2, round 1). A global index would let
-- Tenant B park a pending invite on an email and permanently block Tenant A
-- from onboarding that person, and would raise a UniqueViolation with no user
-- row whose tenant could be named. Cross-tenant collision is instead caught by
-- the users lookup at send time, which is the only check with a real account
-- behind it. COALESCE gives the NULL (superadmin) scope its own slot.
CREATE UNIQUE INDEX IF NOT EXISTS user_invites_pending_email_idx
  ON user_invites (COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid), lower(email))
  WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS user_invites_tenant_idx ON user_invites (tenant_id, status, created_at DESC);
```
`audit_log` unchanged: entity_type `invite`, action `created` (send) / `updated` (resend, revoke, accept), via `audit.write_audit` on the same connection. `token_hash` is added to `audit.py`'s redaction set so it never lands in Postgres.

## Interfaces
**Permission contract.** Every mutating admin path calls exactly `may_invite(actor_role=jwt.role, actor_tenant_id=jwt.tenant_id, target_role=…, target_tenant_id=…)`, raising 403 on `False`. Create passes the *body's* role/tenant; resend and revoke pass the *loaded row's* (404 first if absent). No router compares tenants itself. `may_invite` returns `False` for `supervisor`/`agent` actors unconditionally — belt and braces, since the console gate already 403s them before any router body runs.

```python
# deps.py
CONSOLE_ROLES: frozenset[str]
async def get_authenticated_user(authorization: str | None = Header(default=None)) -> CurrentUser
async def get_current_user(user: CurrentUser = Depends(get_authenticated_user)) -> CurrentUser

# invites.py
def may_invite(*, actor_role: str, actor_tenant_id, target_role: str, target_tenant_id) -> bool
def _new_token() -> tuple[str, str]          # secrets.token_urlsafe(32) -> (raw, sha256 hex)
async def create_invite(*, email, role, tenant_id, team, actor) -> tuple[dict, str]  # row, raw token
async def resend_invite(invite_id, *, actor) -> tuple[dict, str]   # rotates token_hash in place
async def revoke_invite(invite_id, *, actor) -> dict
async def accept_invite(*, raw_token, password) -> dict
    # raises InviteUsed / InviteExpired / InviteRevoked / EmailTaken / LookupError
```

**Token (finding 4, round 1).** `secrets.token_urlsafe(32)` — 256 bits from `os.urandom`, ~43 URL-safe chars; compared as `hashlib.sha256(raw.encode()).hexdigest()` against the unique `token_hash`. Never logged, never echoed in a response body, never written to `audit_log`. Emailed as `${INVITE_BASE_URL}/invite#<token>`.

**Token placement.** The link carries the token in the **fragment**, not a path segment: fragments are never sent to a server, so the raw token cannot reach an access log or a `Referer` header. The accept page reads `location.hash` client-side and sends it in an `X-Invite-Token` header on both `GET /invites/accept` and `POST /invites/accept` — no server route ever has the token in its path. (Chosen over "short expiry, accept the risk" because the 7-day expiry is fixed by the PRD, so a proxy log entry would be a week-long live credential.)

**HTTP.** `POST /invites` (201, invite plus `email_sent: bool`), `GET /invites`, `POST /invites/{id}/resend`, `POST /invites/{id}/revoke`; public `GET /invites/accept` and `POST /invites/accept` `{password}`, token in `X-Invite-Token`. `GET /invites/accept` returns only `{email, tenant_name, role, status}` for a valid token — the invitee's own email and prospective tenant, nothing about any other account. `?tenant_id=` on `GET /invites` and `GET /users` is honored **only for super_admin**; for a tenant-scoped actor it is ignored and forced to the JWT's `tenant_id` (CURSOR.md: never trust client tenancy).

### Conflict responses (round-1 CRITICAL, kept)
The send path lower-cases the email and looks it up in `users` (live rows only). The response depends on *who is asking*:
- **tenant_admin**, existing live account in **any** tenant, or an existing pending invite in their own tenant: `409 {"detail": "this email cannot be invited"}` — one flat string, byte-identical across all those cases. No tenant name, slug, id or role.
- **super_admin**: `409 {"detail": "email already belongs to tenant '<slug>'"}` — the platform operator already sees every tenant via `GET /users`.

`create_invite` maps an asyncpg `UniqueViolationError` on `user_invites_pending_email_idx` (concurrent double-send within one tenant) to the *same* 409 the pre-check would have produced for that actor.

**PRD amendment.** AC9's "rejected with a conflict error naming the existing tenant" is amended in parallel to "rejected with a conflict error; the error names the existing tenant only when the actor is a super_admin." The design implements the amended text.

### Probe rate limit (finding 2 — the conflict is still an oracle)
A flat 409 still answers "does this address have an account somewhere on the platform" one bit at a time, and the round-1 cap counted *sends*, so a tenant_admin whose every attempt 409s was never throttled at all. Two counters, both keyed on `invited_by` (the JWT subject), both checked **before** the users lookup runs:
- **30 invite-create attempts per actor per hour**, counting every outcome — 201, 409, 403 and 429 alike. This is the probe cap; it is deliberately outcome-blind so that a conflict costs the attacker exactly what a success costs.
- **20 successful sends per actor per hour** across create + resend (the mail-bomb cap), and a **60-second per-invite resend cooldown** off `last_sent_at` → `429 "invite was just sent; try again in Ns"`.

Both return 429 with a `Retry-After`. 30/hour bounds enumeration at ~720 addresses/day per compromised admin account, which is slow enough to be an audit-log signal rather than a scrape; every 429 and every 409 is already an `audit_log` row via the create path, so the pattern is visible.

### Accept: atomic, ordered, and squat-safe (findings 3, 5, 6)
`accept_invite` runs one transaction with the conditional UPDATE **first**, so the invite row — not the users unique index — is the serialisation point:

1. `SELECT * FROM user_invites WHERE token_hash = $1` — classification only (missing → 404; `revoked` → 410 revoked; `accepted` → 410 already used; `expires_at < now()` → 410 expired).
2. `UPDATE user_invites SET status='accepted', accepted_at=now(), updated_at=now() WHERE id=$1 AND status='pending' AND expires_at > now() RETURNING *`. **`accepted_user_id` is deliberately not set here** — setting it would have forced the users INSERT to happen first, which is exactly how the loser of a race ends up serialising on `users_email_lower_idx` and getting a 500 instead of the promised 410 (lesson 8). Zero rows → raise `InviteUsed`; the transaction rolls back and the caller gets 410 "already used".
3. `users._insert_user(conn, …)` on the same connection, with the invite's stored role/tenant/team and the bcrypt hash.
4. `UPDATE user_invites SET accepted_user_id=$2 WHERE id=$1` — same transaction, so the FK is populated atomically with the user row.
5. `audit.write_audit(conn, entity_type="invite", action="updated", …)`.

**Step 3's own conflict (finding 3).** Cross-tenant squatting is closed at create but not at accept: Tenant A and Tenant B may each hold a pending invite for one address, and whichever is accepted second finds the account already taken. That INSERT raises `UniqueViolationError` on `users_email_lower_idx`; `accept_invite` catches it and raises `EmailTaken`, which the router returns as `409 {"detail": "an account already exists for this email"}` — the same tenant-blind wording as the create path, since the accepting party is unauthenticated and must learn nothing about the other tenant. The whole transaction rolls back, so **the invite is not burned**: it stays `pending` and remains resendable/revocable, which is the difference between this and today's unhandled-500 behaviour.

### Throttle key (finding 7)
The accept-route limiter keys on `request.client.host`. That is the correct source *for this deployment*: nothing in `deployment/docker/docker-compose.yml` puts a reverse proxy in front of the Config Service — the Admin UI calls `http://localhost:8000` directly (`admin-ui/lib/api.ts`) — so `X-Forwarded-For` is entirely attacker-controlled and must not be read. If a proxy or ingress is introduced later, the change is: add `ProxyHeadersMiddleware` (or equivalent) with an explicit `trusted_hosts` list and take the right-most untrusted hop from `X-Forwarded-For`; never take the left-most, and never read the header without a trusted-proxy list. A one-line comment in `app.py` at the limiter states this so the migration is not silently botched.

Limits on the public routes: **10 accept requests per IP per minute** and **50 per IP per hour**, covering `GET` and `POST` together, 429 over the limit — and the 429 is byte-identical for a valid and an invalid token. Counters are in-process fixed windows in `app.py`; no Redis dependency is added.

## Risks
- **The console gate lives inside `get_current_user`, which makes it invisible at the call site** — a future maintainer could "simplify" a router onto `get_authenticated_user` and silently reopen the surface. Mitigation: `get_authenticated_user`'s docstring says it is for self-service auth routes only; a test asserts exactly two routes in the whole app depend on it, and fails when a third appears.
- **`CONSOLE_ROLES` is an allowlist that a new role can be omitted from — safely, but silently** — a role added to the CHECK constraint and forgotten here simply gets 403 everywhere, which is the safe default but may look like a bug. Mitigation: a test enumerates the roles in `users_role_check` and asserts each is explicitly classified as console or non-console, so adding a role fails the suite until someone decides.
- **The 28 existing `Depends(get_current_user)` routes are still not individually audited** — this build fences off the two new roles but does not revisit what `viewer` may read (e.g. provider config rows). Mitigation: out of scope and stated as such; the gate means the new roles add zero surface, so nothing regresses relative to today.
- **In-process throttling is per-worker and resets on restart** — a multi-replica deployment multiplies every limit by the replica count. Mitigation: accepted (Config Service runs single-replica today); the limits are a brute-force/mail-bomb/enumeration speed bump layered on a 256-bit token, not the primary control, and they live behind one helper so a Redis-backed version is a drop-in. This is the one new mechanism the PRD did not ask for; it exists because the accept route is public and unauthenticated.
- **Dropping `users_email_key` weakens the constraint on soft-deleted rows** — two soft-deleted rows may now share an address. Mitigation: intended; `get_user_by_email`, `authenticate` and every listing already filter `deleted_at IS NULL`, so a ghost row is unreachable, and this is precisely what makes a departed employee re-invitable.
- **Reported, not redesigned here (round-1 findings 9–11)** — (9) the super_admin/tenant listing-scope split is duplicated between `list_users` and `list_invites` rather than resolved by one shared scope helper; (10) resend/revoke return 403 for another tenant's invite id but 404 for a nonexistent one, a weak existence oracle; (11) accept's password `min_length=8` is the only strength rule. Mitigation: (11) is fixed here (schemas.py row above); (9) and (10) are logged for the follow-up that introduces a shared scope resolver — (10) is bounded by UUID unguessability and reveals only that *an* invite exists.

## Test plan
**Unit (pure, no DB).** `may_invite` — the privilege-escalation matrix: tenant_admin→superadmin **denied** (AC2); tenant_admin→any role in Tenant B, including `tenant_id=None`, **denied** (AC3); tenant_admin→{tenant_admin, supervisor, agent, viewer} in own tenant **allowed** (AC4); superadmin→tenant_admin in any tenant and →superadmin **allowed**; supervisor/agent as *actor* → denied for every target. The same table is replayed with a stored row's role/tenant as the target, covering resend and revoke. Plus: every role in `users_role_check` is explicitly classified as console or non-console.

**Integration** (`services/config/tests/`, existing `conftest.py` fixtures):
- **Console gate (finding 1).** A JWT with role `agent`, and one with `supervisor`, gets 403 on `GET /users`, `GET /provider-configs`, `GET /carriers`, `GET /calls` and `GET /audit-log`, but 200 on `GET /auth/me` and 204 on `POST /auth/change-password`. A `viewer` JWT still gets 200 on those reads — **including a `viewer` with `is_service_account=true` and `tenant_id` NULL, the shape `scripts/create_service_account.py:33` creates**, proving Conversation/Knowledge are not locked out. Unauthenticated `GET /health`, `GET /auth/setup-status`, `POST /auth/login`, `POST /auth/bootstrap`, `GET /invites/accept` and `POST /invites/accept` all still reach their handlers (no 401/403 from the gate) — `/health` asserted explicitly because docker-compose's healthcheck gates two other services on it. And: exactly two routes in the app depend on `get_authenticated_user`.
- **Lifecycle.** Accept creates a user with exactly the invite's role/tenant/team, ignoring the body (AC5); expired invite → 410 "expired" (AC6); resend rotates the hash so the old token 404s and the new one accepts (AC7); revoke blocks accept and leaves an accepted invite's user intact (AC8); replaying an accepted token → "already used", asserted as a *different* detail from expired and revoked (AC10); SMTP patched to raise → 201 `email_sent: false`, row still `pending`, resend succeeds (AC12).
- **Cross-tenant leakage.** A tenant_admin inviting an email that exists in *another* tenant gets exactly `"this email cannot be invited"`; assert the body contains neither the other tenant's slug, name nor id, and that it is byte-identical to the response for an email with a pending invite in the actor's own tenant. The same request as super_admin does name the tenant.
- **Probe limit (finding 2).** 30 consecutive create attempts that all 409 → the 31st is 429; a *successful* create after 30 conflicts is also 429, proving the counter is outcome-blind. Independently: 20 successful sends → the 21st is 429 even though the probe counter has room.
- **Squatting at accept (finding 3).** Tenant B and Tenant A each hold a pending invite for `x@e.com`; the first accept 201s, the second returns **409 "an account already exists for this email"** — not 500 — and that second invite is still `pending` afterwards and can be revoked. Assert the 409 body names no tenant.
- **Soft-delete re-invite (finding 4).** Invite → accept → `DELETE /users/{id}` (soft) → the same address can be invited again and accepted, producing a second live row; assert the old row is still present with `deleted_at` set and that `authenticate()` with the old password fails.
- **Backfill guard (finding 5).** Against a fixture DB seeded with `Bob@x.com` and `bob@x.com` both live, applying `schema.sql` raises the named exception and leaves the table unmodified; with only one of them, it applies cleanly and lower-cases the row.
- **Concurrency (finding 6).** Two simultaneous `POST /invites/accept` with the same token → exactly one 201 and one **410 "already used"** (asserted specifically, since a 409/500 here means the ordering regressed), and exactly one row in `users`. Two simultaneous creates for one email in one tenant → one 201, one 409.
- **Token and IP throttle (findings 4 round 1, 7).** `_new_token()` returns ≥43 chars with no collision in 1000 calls; the raw token appears in no response body, log record or `audit_log` row; the 11th accept attempt from one IP in a minute → 429, byte-identical for a valid and an invalid token; a request carrying a forged `X-Forwarded-For` is throttled on the same bucket as one without it, proving the header is ignored.
- **Cooldown.** Two resends within 60s → the second is 429 naming the remaining seconds.
- **Normalization (finding 8, round 1).** An account created as `bob@x.com` blocks an invite to `Bob@X.com`; accept for an invite stored from `BOB@x.com` resolves to the same lower-cased row and creates exactly one user.
- **Scoping.** `GET /invites` as tenant_admin returns only Tenant A — including when it passes `?tenant_id=<Tenant B>` — and all tenants for superadmin (AC11); tenant_admin resend/revoke of a Tenant B invite id, and of a superadmin invite → 403, not 204 (IDOR).
- Plus one assertion that each mutating path wrote an `audit_log` row in the same transaction, with `token_hash` redacted.
