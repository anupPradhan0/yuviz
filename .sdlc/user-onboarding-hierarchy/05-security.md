# Security review: user-onboarding-hierarchy (complete feature, Phases 1-4)
VERDICT: GREEN

**Re-verified 2026-09-06:** the round-2 medium (cleartext SMTP AUTH) is fixed and closed. Only the
STARTTLS change was re-audited; the rest of the feature is unchanged from the assessment below.

Scope: whole working tree on `feature/user-onboarding-hierarchy` — `git diff` plus the untracked
`services/config/{invites,email}.py`, `services/config/routers/invites.py`,
`admin-ui/app/{users,invite}/page.tsx`, and `tests/test_{invites,invite_routes,throttle,email,
console_gate}.py`. This supersedes the Phase 1+2 report. The conditional GREEN is now unconditional:
T18 landed and the probe cap is genuinely outcome-blind (verified below, including at the wire).

## Findings

1. [low] STARTTLS is negotiated with an unauthenticated TLS context, so an *active* on-path
   attacker can still capture the SMTP credential and the invite token — `services/config/email.py:69`
   `smtp.starttls()` is called with no `context=`. In this Python (3.14), `smtplib.SMTP.starttls`
   falls back to `ssl._create_stdlib_context()`, which is `verify_mode=CERT_NONE,
   check_hostname=False` — the session is encrypted but the relay is not authenticated.
   Attack: an attacker who can already redirect or intercept the Config-Service→relay TCP
   connection (ARP/DNS spoofing on the container network, a hostile sidecar) answers the STARTTLS
   with any self-signed certificate; `wrap_socket` accepts it, `login()` then sends the resolved
   `SMTP_PASSWORD_REF` secret and the invite body — with its live, bearer-equivalent token — inside
   a TLS session the attacker terminates. This is strictly narrower than the closed medium: passive
   eavesdropping is now defeated, and STARTTLS *stripping* is not a leak either (the pre-TLS
   `has_extn("starttls")` check raises `SMTPNotSupportedError` rather than falling back), so it
   requires an active MITM position.
   Fix: `smtp.starttls(context=ssl.create_default_context())` — one argument, and the existing
   no-try/except stance makes a verification failure a clean send failure automatically.

2. [low] `GET /invites/accept` does not run accept's granting-context re-validation, so a dead
   invite still discloses its tenant's display name for up to 7 days —
   `services/config/invites.py:390-417` vs `accept_invite`'s checks at `:300-320`
   The read-only path classifies revoked/accepted/expired only; it never checks that the target
   tenant is live, that the inviter is live, or that `may_invite` still holds. It then resolves
   `tenants.get_tenant_by_id(...)["name"]` and returns it.
   Attack: an offboarded tenant_admin who, before departure, issued an invite to an address they
   control keeps the emailed token. `POST /invites/accept` correctly 410s (`InviteContextGone`), but
   `GET /invites/accept` keeps returning `{email, tenant_name: "Acme Corp", role: "admin"}` to them
   unauthenticated, from any IP, for the rest of the 7-day TTL — surviving the soft-delete of both
   the inviter and the tenant. What they gain is small (an internal tenant display name plus the
   role they themselves chose), which is why this is low, not the mirror of the round-1 critical.
   Fix: run the same tenant-live / inviter-live / `may_invite` checks in `get_invite_for_accept`
   and raise `InviteContextGone`, so GET and POST agree on what a dead invite looks like.

3. [low] `resend_invite` rotates the token of an already-*expired* invite without extending
   `expires_at` — `services/config/invites.py:205-217` (carried forward, previously reported,
   unfixed)
   Expiry is derived, so an expired row is still `status='pending'` and passes the guard at :205.
   Attack: a tenant_admin resends a 9-day-old invite, gets a 200 and `email_sent: true`, and
   believes onboarding was re-sent; the invitee receives a fresh link that 410s. It is also a free
   real-email-send primitive on a permanently dead row, bounded by the 20/hour send cap.
   Fix: `SET expires_at = now() + interval '7 days'` in the resend UPDATE, or reject expired rows
   with `InviteNotPending`.

4. [low] `may_invite` treats a NULL actor tenant as an ordinary tenant, and deleting `POST /users`
   made invites the only account-creation path — `services/config/invites.py:108-111`
   (carried forward, previously reported, unfixed; blast radius now slightly larger)
   `_same_tenant(None, None)` is True, so an actor with `role="admin", tenant_id=NULL` passes the
   admin branch for `target_tenant_id=NULL`.
   Attack: a superadmin creates one platform-scope admin (legitimate today). That admin can now
   mint further platform-scope `admin` and `viewer` accounts indefinitely with no superadmin in the
   loop — including the `viewer` + `tenant_id=NULL` shape that other services treat as a
   service-account identity. Persistence and delegation, not elevation: they cannot reach
   `superadmin` (:109-110) and cannot reach any real tenant (:111).
   Fix: `if actor_role == "admin": return actor_tenant_id is not None and target_role !=
   "superadmin" and _same_tenant(...)`. `TestMayInvite` still never parametrises a NULL actor
   tenant, so this combination remains unexercised.

5. [low] 403 for another tenant's invite id vs 404 for a nonexistent one on resend/revoke —
   `services/config/invites.py:196-204`, `:236-244` (carried forward; explicitly accepted as design
   round-1 finding 10, `02-design.md:188`)
   Confirmed unchanged at the wire: the row is loaded first, `LookupError` → 404 via
   `app.py:202`, then `may_invite` → 403 via `app.py:234`. 404-before-403 ordering is correct and
   deliberate; the residual is that a tenant_admin holding an invite UUID obtained out of band can
   confirm it exists. Bounded by UUIDv4 unguessability. Recorded, not re-raised.

6. [low] Pending invites are not revoked when their tenant or their inviter is soft-deleted —
   `services/config/tenants.py:148`, `services/config/users.py` (carried forward, unfixed)
   Security-neutral because accept re-validates, but dead invites remain creatable, are still
   emailed to real people, and now appear in `GET /invites` and the Users page's Pending table with
   nothing distinguishing them from live ones. See also finding 2, which is the same root cause.

7. [low] The inviter row is read without `FOR UPDATE` in `accept_invite` —
   `services/config/invites.py:310-313` (carried forward, unfixed). Under READ COMMITTED an
   in-flight uncommitted offboarding UPDATE is invisible, so an accept landing in that
   millisecond-wide window still succeeds. Not practically steerable.

## The probe oracle (T18) — verified closed as designed

The round-1 medium was that `create_invite`'s existence pre-check is platform-global, making 409 vs
201 a cross-tenant account-existence oracle. Checked every dimension the task named:

- **Attempt-counter consumption is identical.** `routers/invites.py:47` calls
  `check_probe(current_user.id)` as the *first* statement of the handler body, before
  `check_send_cap` and before `invites_service.create_invite` runs its `get_user_by_email` lookup.
  `InviteThrottle.check_probe` (`app.py`) peeks then increments unconditionally — a 201, a 409 from
  the pre-check, a 409 from the `UniqueViolationError` path, a 403 from `may_invite` and a 429 from
  the send cap all consume exactly one attempt. 30/hour, matching the design.
- **No path reaches the existence check without incrementing.** `create_invite` is the only caller
  of `users_service.get_user_by_email` on any invite route, and `POST /invites` is its only HTTP
  entry point. Resend and revoke perform no email lookup at all. `GET /users` and `GET /invites`
  are tenant-forced (below). `POST /invites/accept`'s `EmailTaken` 409 requires possession of a
  valid 256-bit token. There is no second door.
- **Response shape is identical.** For any non-superadmin actor `_conflict_tenant_name` returns
  `None` at `invites.py:133-134` *before any DB call*, so `app.py:239`'s handler emits the single
  flat string `{"detail": "this email cannot be invited"}` — byte-identical for an own-tenant
  account, a cross-tenant account, and a concurrent own-tenant duplicate. No tenant name, slug, id
  or role. The superadmin branch names the slug, which is correct: a superadmin already sees every
  tenant via `GET /users`.
- **Timing is not a side channel here.** The tenant-blind branch returns before `get_tenant_by_id`,
  so own-tenant and cross-tenant conflicts do the same number of round trips (one). The 201 path is
  slower than the 409 path (insert + a 10s-bounded SMTP call), but the status codes already
  differ — timing adds no bit that the response does not, and the cap is what bounds the bit rate.
- **The test would fail against the un-capped code.** `test_31st_create_attempt_is_429_and_
  outcome_blind` drives 30 real HTTP 409s and then asserts a *fresh, unused* email 429s. If the
  counter recorded only successful sends, those 30 conflicts would count zero and the 31st request
  would be 201 — the assertion fails. It is a check that can fail (lesson 12). Setup seeds through
  `invites_service` rather than HTTP so it does not itself consume quota.

Residual, unchanged and design-accepted: at 30/hour the oracle is throttled, not eliminated
(~720 addresses/day per compromised admin), and the counters are in-process fixed windows that
reset on restart and multiply by replica count. Both are stated in `02-design.md:160-164` and in
`FixedWindowCounter`'s own docstring.

## Verified controls

1. **Console gate not regressed across three phases.** `deps.get_current_user` is still
   `Depends(get_authenticated_user)` + `role not in CONSOLE_ROLES → 403`, and `require_role()._check`
   still depends on `get_current_user`, so a router's own `dependencies=` cannot bypass it. Repo-wide,
   the only non-test callers of `get_authenticated_user` remain `deps.py`, `/auth/me` and
   `/auth/change-password`. The new invite router adds no third bypass.
2. **The accept routes bypass the gate by construction, not by exemption.** `routers/invites.py`'s
   `GET /invites/accept` and `POST /invites/accept` declare no identity dependency at all — no
   `get_current_user`, no `get_authenticated_user` — so lesson 1's failure mode (an auth-optional
   guard creating an unauthenticated passthrough) does not arise. Asserted at the wire: a request
   with no `Authorization` header reaches the handler and returns 404 for a bad token, not 401/403.
3. **`may_invite` is still the single decision point** and is called by create (with the body's
   role/tenant), resend and revoke (with the *stored* row's role/tenant, loaded `FOR UPDATE`), and
   accept (with the inviter's *current* role/tenant). No router compares tenants itself; no inline
   `if`-chain survives. The listing and delete-equivalent (revoke) paths are both covered.
4. **`?tenant_id=` is forced, not validated, on both listing routes.**
   `routers/invites.py:77` and `routers/users.py:20`: `tenant_id if role == "superadmin" else
   current_user.tenant_id`. A tenant_admin passing `?tenant_id=<Tenant B>` is silently rescoped to
   their own tenant. The service functions never trust a caller value on their own: `list_invites`
   and `list_users` only widen to all tenants on `is_superadmin and tenant_id is None`, and
   `is_superadmin` is derived from the JWT role, never from whether a filter was supplied. A
   non-superadmin also gets the `role != 'superadmin'` row exclusion regardless of tenant.
5. **`token_hash` never leaves the server.** `invites.to_public_dict` strips it from every invite
   response; `audit._SECRET_REF_FIELDS` now includes `token_hash`, covering the resend path that
   writes it into both `old_value` and `new_value`; the raw token is returned only to the router,
   which passes it to `email.send_invite_email` and nowhere else. `grep` for `log`/`logger`/`print`
   in `invites.py`, `email.py` and `routers/invites.py` returns nothing — the raw token, the SMTP
   password and the SMTP failure detail all reach zero log lines. `users.to_public_dict` strips
   `password_hash` from the accept response.
6. **Token placement is fragment-only, end to end.** `email.py:68` builds
   `{INVITE_BASE_URL}/invite#{raw_token}` — a fragment, never sent to a server, so it cannot land
   in an access log or a `Referer`. `admin-ui/lib/api.ts` carries it as `X-Invite-Token` on both
   accept calls, never a path or query segment. `admin-ui/app/invite/page.tsx:27` reads
   `window.location.hash` into React state only; the token appears in no `router.push` (the only
   push is a literal `"/login"`, which drops the fragment), no `<a href>`, no analytics call, no
   `console.*`, and no rendered text. Residual, inherent to the design: the token sits in browser
   history and in the mail body — both accepted, both stated in the design.
7. **The invite page's error rendering is safe.** Both `loadError` and `formError` are the API's
   `detail` string interpolated as JSX text, so React escapes it — no `dangerouslySetInnerHTML`
   anywhere in `admin-ui/app/invite/` or `admin-ui/app/users/`. Traced every `detail` that page can
   render: the four 410 strings, the tenant-blind 409, the 404, and the 429 — all fixed literals
   set in `app.py`'s handlers, none containing a tenant name, an account, or an id. The one handler
   that echoes an exception message (`app.py:202-204`, `str(exc)`) reaches this page only as
   `"invite not found"`.
8. **The Users page does not filter client-side for tenant scope.** It calls `listUsers()` and
   `listInvites()` with no `tenant_id` argument and renders the server's rows as-is; the only
   client-side conditional is `isSuperadmin` hiding the *Tenant column*, which is cosmetic on data
   the server already scoped. The invite form sends `tenant_id: isSuperadmin ? tenantId : current
   User.tenant_id`, and `may_invite` re-decides server-side regardless. The `canManageUsers` nav
   gate mirrors `require_role("superadmin","admin")` and is presentation only.
9. **IDOR on resend/revoke, at the wire.** Row loaded by primary key → `LookupError` → 404
   (`app.py:202`) → `may_invite` against the stored row → `PermissionDenied` → 403 (`app.py:234`).
   404 precedes 403, and the 403 detail is a fixed string naming neither the invite nor a tenant.
   `test_idor_resend_and_revoke_of_another_tenants_invite_is_403` asserts both at HTTP level.
10. **Accept remains atomic and correctly ordered.** `SELECT ... FOR UPDATE` → classification →
    granting-context re-validation → conditional `UPDATE ... WHERE status='pending' AND expires_at
    > now() RETURNING *` (zero rows = loser) → `_insert_user` → `accepted_user_id` backfill. The
    backfill is still deliberately after the insert (lesson 8), and expiry is still enforced
    server-side by the database's `now()`, not only the Python classification. The real-Postgres
    concurrent-accept test still asserts exactly one user and one `InviteUsed`.
11. **The deferred-grant revalidation found in round 1 has not regressed** (lesson 16). All three
    checks — target tenant live, inviter live, `may_invite` re-run against the inviter's *current*
    role/tenant — are still inside the transaction, still under the invite row's `FOR UPDATE` lock,
    still before the conditional UPDATE, so a failure rolls back with the invite `pending` and no
    user row. `InviteContextGone` still carries no payload and `app.py:280` maps it to one flat
    410, so its three causes stay indistinguishable.
12. **`EmailTaken` is tenant-blind at the wire** (`app.py`, `"an account already exists for this
    email"`) and still does not burn the invite — the `UniqueViolationError` rolls the transaction
    back and the row stays pending/resendable.
13. **Accept-route IP throttle keys on `request.client.host`.** `X-Forwarded-For` is read nowhere in
    the service (grepped); a forged value provably does not change the bucket. `_client_host`
    handles `request.client is None` with a fixed sentinel, so such a request shares a real bucket
    rather than skipping the limiter or 500ing — both asserted. 10/min + 50/hour cover GET and POST
    together and the 429 is a fixed string with `Retry-After`, byte-identical for a valid and an
    invalid token (asserted both ways).
14. **`FixedWindowCounter` has no TOCTOU.** `over_limit` and `increment` contain no `await`, so the
    peek-then-increment pair cannot be interleaved by another task on the event loop. Stale keys are
    swept on every access, so the IP-keyed map on the two public routes is bounded by the current
    window rather than by process lifetime.
15. **SMTP does not block the event loop and cannot hang** (lessons 18, 19): the blocking
    `_send_sync` runs via `asyncio.to_thread` with an explicit `timeout=10`, which is the value the
    design named. Diffed the design's other named controls one by one — 30/hour probe, 20/hour send,
    60s cooldown, 10/min + 50/hour accept, `X-Invite-Token`, fragment link, `min_length=8`,
    `email_sent: bool`, forced `?tenant_id=` — all present in code. The only design-named control
    with no implementation is transport security for SMTP, which the design never named either.
16. **STARTTLS is on by default and cannot fail open** (re-verified). `_send_sync` does
    connect → `starttls()` → `login()` → `send_message()` as straight-line code inside the one
    blocking call, so no config combination can reorder it and the upgrade stays off the event loop
    (lesson 18); `timeout=10` is unchanged. There is no `try`/`except` around `starttls()` and no
    cleartext branch, so every failure mode reaches `login()` never: a relay that does not advertise
    STARTTLS raises `SMTPNotSupportedError` before any command, a relay answering non-220 raises
    `SMTPResponseException`, and a handshake that fails midway raises out of `wrap_socket` — in all
    three the `with` block exits and the credential is never transmitted. **The default is secure by
    construction**: `_env_bool` returns `default=True` for unset *and* for empty/whitespace, and
    disables only on an explicit `false`/`0`/`no` after `.strip().lower()` — so `"False"`,
    `"FALSE"`, `" 0 "` disable as intended while any unrecognised value (`"off"`, `"disabled"`,
    `"maybe"`, a typo) falls through to **on**, not off. **The opt-out cannot be set accidentally**:
    grepped `deployment/`, `docs/` and `scripts/` — `SMTP_STARTTLS` appears in exactly one place,
    `docs/setup.md:148`, set to `"true"`, with prose stating it must stay true outside local dev.
    There is no `.env.example`, no compose default and no k8s manifest carrying any `SMTP_*` value.
    **AC12 still holds**: a STARTTLS failure propagates out of `send_invite_email` into the router's
    `except Exception`, which sets `email_sent: false` — the invite row stays committed `pending`
    and resendable, and no 500 reaches the client. The three new tests are correctly shaped:
    starttls-before-login is asserted on call *ordering* (not merely that it was called), the
    refusing-relay test asserts `login`/`send_message` were never reached, and both were
    mutation-verified.
16. **Email header injection is not reachable.** `InviteCreate.email`'s pattern
    `^[^@\s]+@[^@\s]+\.[^@\s]+$` excludes `\r` and `\n` from every segment, so no `Bcc:`/`X-` line
    can be smuggled into the `To` header; no user-controlled display name is placed in any header
    (`From` comes from `SMTP_FROM`, the subject is a literal). `team` is user-controlled but is
    stored only, never rendered into the message.
17. **SMTP secrets go through the secret-ref convention**, not a bare env var:
    `SMTP_PASSWORD_REF` resolved via `CompositeSecretResolver`, matching every other provider
    credential. `docs/setup.md` documents the ref, and the resolved value is never logged, never
    audited, and never returned.
18. **An SMTP failure is non-fatal and leaks nothing.** The router's `except Exception` sets
    `email_sent = False` and writes no log line, so a relay hostname, an auth error or a
    misconfigured env var name never reaches a client or a log. The row stays committed `pending`
    and resendable (asserted).
19. **No privilege escalation through the widened role enum** (lesson 4). `PATCH /users/{id}` and
    `DELETE /users/{id}` are still `require_role("superadmin")`, so widening `UserUpdate.role` to
    include `supervisor`/`agent` does not let an admin edit roles. `supervisor`/`agent` are fenced
    out of the whole Config surface by `CONSOLE_ROLES`, and `may_invite` denies them as actors
    unconditionally. A tenant_admin cannot invite a `superadmin` (`:109-110`) or into another
    tenant (`:111`).
20. **`POST /users` deletion has no orphaned caller** (lesson 17). `createUser`/`UserCreate` appear
    nowhere in `admin-ui/app`, `admin-ui/lib` or `admin-ui/components`; `settings/page.tsx` was
    rewritten in the same change.
21. **Schema constraints scoped correctly** (lesson 3): `user_invites_pending_email_idx` is on
    `(COALESCE(tenant_id, <sentinel>), lower(email)) WHERE status='pending'`, so Tenant B cannot
    park an invite to block Tenant A, and the NULL/platform scope gets its own slot. The role
    CHECK drop+re-add is one `EXECUTE` pair inside a `DO $$` block, so the table is never left with
    no role constraint (lessons 10, 13).
22. **No string-built SQL** in any file in this change; every statement is parameterised. The one
    f-string interpolates the module-level `INVITE_TTL` constant. A malformed `tenant_id` or
    `invite_id` surfaces as asyncpg's `ValueError` → a 400, not a 500 with a traceback.

## Not provable at this commit

- **`admin-ui` has no test infrastructure at all** — no runner, no test files, no CI step. Every
  frontend claim above (controls 6, 7, 8) is from reading `invite/page.tsx`, `users/page.tsx`,
  `lib/api.ts` and `AppShell.tsx` in full, not from an executed assertion. Specifically unproven:
  that the token never escapes the fragment under a future edit, and that no error state renders a
  server string unescaped. A single Playwright case asserting the accept flow never emits the
  token in a network URL would convert the highest-value one of these into a regression test.
- **No wire-level test asserts the 409 body is byte-identical across the tenant boundary.** The
  tenant-blindness is tested at the exception level (`test_invites.py:244-291` asserts
  `tenant_name is None` for a tenant_admin actor in both the own-tenant and cross-tenant cases),
  and `app.py`'s handler is a two-branch function on that field, so the wire behaviour follows by
  inspection. The design's own test plan (`02-design.md:198`) called for asserting the 409 body
  names no tenant; that assertion exists only for the accept-path `EmailTaken`, not for the
  create-path conflict. A missing test, not a missing control.
