# Security review: PR #19 fixes (uncommitted working tree, feature/user-onboarding-hierarchy)
VERDICT: AMBER

Scope: `git diff` of the 15 modified files, judged against `02-design.md`, `01-prd.md` and the
prior GREEN audit (`05-security.md`). Finding 1 of the prior audit is CLOSED and was not re-raised;
lows already recorded as accepted there are not repeated.

No critical or high is open. The round-1 tenant-blind-conflict CRITICAL stays closed. Three issues
below are real but bounded; AMBER rather than GREEN because finding 1 is an authorization widening
with no consumer, which is a decision the owner should make deliberately rather than inherit.

## Findings

1. [medium] `GET /users` platform-scope widening hands NULL-tenant `viewer` service accounts a
   full cross-tenant user roster, including `superadmin` rows — `services/config/routers/users.py:23-27`,
   `services/config/users.py:74-84`
   The predicate swap itself is correct per lesson 24 (scope is `tenant_id IS NULL`, not
   `role == "superadmin"`). The problem is the *route*: `GET /users` is gated only by
   `Depends(get_current_user)` (CONSOLE_ROLES), so the widened branch is reachable by any
   NULL-tenant actor of *any* console role. Enumerated holders of `tenant_id IS NULL`: bootstrap
   superadmins, and the `role="viewer"` service accounts (`conversation-service@internal.yuviz.ai`,
   vobiz) created directly in Postgres. Before this change a NULL-tenant viewer got
   `tenant_id IS NOT DISTINCT FROM NULL AND role != 'superadmin'` — platform rows only, superadmins
   excluded. Now it gets `SELECT * FROM users` across every tenant with no role exclusion.
   Attack: an attacker who obtains a service-account JWT (long-lived, non-human, sitting in another
   service's environment — a leaked env dump, a container escape, an SSRF against Conversation)
   previously could read platform-level non-superadmin rows; it now enumerates every user in every
   tenant — email, role, team, tenant_id — plus the identity of every superadmin, which is a
   ready-made target list for the credential path that has no lockout. Grepping `services/conversation`,
   `services/vobiz` and `scripts/` finds no caller of `GET /users` at all, so nothing today needs
   this reach: the widening buys no functionality and costs a PII blast radius.
   Fix: keep `is_platform_scoped` as the *tenant* predicate but gate the unscoped branch on
   authority too — `Depends(require_role("superadmin", "admin"))` on the route, or retain the
   `role != 'superadmin'` exclusion for any actor whose own role is not `superadmin`.

2. [medium] `FixedWindowCounter._MAX_BUCKETS` fails closed on *new* keys, turning the limiter on the
   two public accept routes into a lockout primitive — `services/config/app.py:114-122`
   At 20,000 tracked keys, `over_limit()` returns `(True, window)` for any key not already in
   `_buckets`, and `AcceptThrottle.check()` raises 429 for both `GET /invites/accept` and
   `POST /invites/accept`. The key is `request.client.host` (correctly *not* `X-Forwarded-For`), so
   filling the map needs real distinct source addresses — but the `hour` counter's window is 3600s,
   so an attacker needs 20k distinct IPs per hour sustained, which a small botnet or a single host
   with an IPv6 /64 supplies trivially if the service is ever exposed beyond the documented
   localhost deployment. Once full, every invitee whose IP is not already a bucket is refused for
   the rest of the window, and the flood keeps it refilled.
   Attack: an unauthenticated attacker sources requests at `GET /invites/accept` from 20k addresses
   and blocks onboarding platform-wide — every new employee's invite link 429s until it expires.
   It is a fair trade against the unbounded O(n)-sweep amplification it replaces, but the failure
   direction is wrong: existing keys should be evicted, not new ones refused.
   Fix: at capacity evict the oldest-`window_start` bucket (or an LRU victim) and admit the new key,
   rather than returning `over=True`; keep the cap as a memory bound, not an admission decision.

3. [low] The expired-invite self-heal ignores the stored invite's *role* and writes no audit row —
   `services/config/invites.py:180-192`
   The `UPDATE ... SET status='revoked'` matches on `(COALESCE(tenant_id, zero-uuid), lower(email),
   status='pending', expires_at <= now())` only. `may_invite` has already forced same-tenant for an
   `admin` actor and cannot revoke a live row, so there is no cross-tenant or live-invite exposure.
   But `revoke_invite` refuses `target_role='superadmin'` and the self-heal does not, and unlike
   `revoke_invite` (`invites.py:291-300`) it emits no `audit.write_audit` entry.
   Attack: a tenant_admin of tenant A, seeing an expired `superadmin`-role invite for bob@x.com in
   its own tenant, posts `create_invite(email=bob@x.com, role="viewer", tenant_id=A)` and flips that
   row to `revoked` — an action `POST /invites/{id}/revoke` would have 403'd — with no audit trail
   naming them. The row was already expired and unacceptable, so nothing live is destroyed; the loss
   is attribution, not authorization.
   Fix: add `AND role = $3` (the role being created) to the self-heal predicate, or leave the
   predicate and write an audit row per revoked id inside the same transaction.

4. [low] Moving bcrypt to `asyncio.to_thread` makes the pre-existing login user-enumeration timing
   delta easier to measure — `services/config/users.py:186-191`
   `authenticate()` still returns `None` before any bcrypt call when the email is unknown, so a hit
   costs ~250ms of hashing and a miss costs one indexed SELECT. That asymmetry predates this diff,
   but the change removes the event-loop serialization that used to add noise to every concurrent
   request, so the two paths are now cleanly separable by wall clock. `POST /auth/login` has no
   throttle and no lockout (`routers/auth.py` contains no rate-limit code), so the oracle is
   unlimited.
   Attack: an unauthenticated attacker times `POST /auth/login` with a wrong password against a
   candidate address list and learns which addresses have platform accounts — no tenant attribution,
   but a precise target list for phishing or credential stuffing.
   Fix: on the `user is None` path, run `auth.verify_password` against a fixed dummy bcrypt hash in
   the same `to_thread` call before returning `None`; separately, add a per-IP counter on
   `/auth/login`.

## Verified controls

- **Round-1 CRITICAL (tenant-naming conflict) stays closed.** `_conflict_tenant_name` returns `None`
  for any non-superadmin actor *before* any DB lookup, so the tenant_admin 409 body is byte-identical
  and equal-cost for an own-tenant vs cross-tenant account collision. `check_probe` increments
  unconditionally before `create_invite` runs, so a 409 costs the actor exactly what a 201 does.
- **`PendingInviteConflict` is not a new oracle.** `user_invites_pending_email_idx`
  (`database/schema.sql:329-331`) is `(COALESCE(tenant_id, zero-uuid), lower(email)) WHERE
  status='pending'` — genuinely per-tenant, so another tenant's pending invite cannot collide.
  `may_invite` runs first and forces `target_tenant_id == actor_tenant_id` for an `admin`, so an
  admin can only reach this exception for a slot inside its own tenant, whose contents `GET /invites`
  already shows it. Only a superadmin can target the NULL-tenant/zero-uuid platform slot, and a
  superadmin sees every invite anyway. The account-exists check (`get_user_by_email`) runs before the
  transaction and still raises the tenant-blind `EmailConflict`, so the two 409s distinguish
  "account exists somewhere" from "live invite exists *here*" — and "here" is always the actor's own
  tenant. No cross-boundary information crosses.
- **Expired-invite self-heal.** Predicate requires `status='pending' AND expires_at <= now()` — a
  live invite is untouched. Tenant matching uses the same `COALESCE(tenant_id, zero-uuid)` expression
  as the index, parameterized, so it cannot reach another tenant's rows. It sits inside the same
  `async with conn.transaction()` as the INSERT, so a `PendingInviteConflict` rolls the revocation
  back. Email is lower-cased and bound as `$2`.
- **`may_invite` platform-scope closure.** `actor_role == "admin" and actor_tenant_id is None` now
  returns `False` unconditionally, so a NULL-tenant `admin` can no longer invite into the platform
  slot (previously `_same_tenant(None, None)` was `True`, self-perpetuating platform admins). The
  `superadmin` branch is unchanged; no other role gains anything.
- **`get_invite_for_accept` context re-validation.** `_check_context_live(pool, invite)` now runs
  *before* `tenant_name` is computed, so the unauthenticated GET no longer discloses a tenant name
  for an invite whose tenant was soft-deleted or whose inviter was demoted/removed. It raises the
  same `InviteContextGone` the POST path raises, handled at `app.py:336` as a single tenant-blind
  410 "invite is no longer valid" — the caller cannot distinguish tenant-gone from inviter-gone from
  inviter-demoted. Read-only against the pool, no lock needed, and identical logic to the locked
  accept path (one function, both callers).
- **`is_platform_scoped` in `routers/invites.py:list_invites`.** Route is still
  `require_role("superadmin", "admin")`, so no NULL-tenant viewer reaches it; for the actors who do
  reach it the predicate is equivalent to the old role check. `?tenant_id=` is still *forced* to the
  JWT tenant for a tenant-scoped actor, not merely validated.
- **Privilege-escalation paths unchanged.** `PATCH /users/{id}` and `DELETE /users/{id}` remain
  `require_role("superadmin")`, so `tenant_id` cannot be set to NULL by an admin — the set of
  NULL-tenant actors is still superadmins plus DB-managed service accounts.
- **`resend_invite` expiry extension.** `INVITE_TTL` is the module constant `"7 days"`
  (`invites.py:34`), not env- or request-derived, so the f-string interpolation into the UPDATE is
  not an injection sink; every other value is a bind parameter. `may_invite` is re-run against the
  actor's *current* role/tenant and the *stored* invite role/tenant before the rotation, and
  `_check_context_live` re-runs at accept, so a resurrected invite is not a stale grant.
- **Token handling unchanged and sound.** Token is `secrets.token_urlsafe(32)`, only the SHA-256 hash
  is stored, and it travels in `X-Invite-Token` — never a path or query segment.
- **`AppShell.tsx` redirect is not load-bearing.** `deps.CONSOLE_ROLES` (`deps.py:36,52`) 403s
  `supervisor`/`agent` inside `get_current_user`, which every Config route depends on; the client
  redirect only prevents lesson-22's error-banner welcome screen. Clearing local state is not relied
  on for any access decision. (Cosmetic only: on `/no-access` itself the guard's condition is false,
  so a non-console role still renders the full sidebar there.)
- **`email.py` outer `asyncio.wait_for`.** The 10s bound now covers connect+starttls+login+send as a
  whole rather than each socket op, and the router's non-fatal `except Exception` around
  `send_invite_email` still catches `asyncio.TimeoutError`, so a black-holing relay cannot hold a
  request open or 500 the create.
- **`UserCreate` schema removal.** `schemas.py` no longer carries the `tenant_id`-bearing
  account-creation model; grep confirms no remaining reference, so no route accepts a client-supplied
  tenant on a user-creation body.
