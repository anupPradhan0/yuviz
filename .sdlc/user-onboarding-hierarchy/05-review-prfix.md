# Code review (round 2 — re-review of the PR-fix round)

VERDICT: GREEN

Both blocking findings from round 1 are genuinely fixed; three minors remain, none blocking.

1. [minor] Post-flood recovery is delayed by up to `_SWEEP_INTERVAL` refusals —
   `services/config/app.py:120` — fails when: a 20,000-IP flood on `/invites/accept` ends and
   the 60s window passes. The latch is gone (`_maybe_sweep` now runs unconditionally before
   `_at_capacity_for_new_key`, so a sweep is always reachable), but above `_SWEEP_THRESHOLD`
   it only fires every 500th access, and a request refused at capacity makes exactly one
   access (`minute.over_limit` short-circuits `check()`), so ~499 legitimate invitees are
   still 429'd after every bucket is already stale. `test_limiter_recovers_after_a_flood…`
   hides this by setting `_SWEEP_INTERVAL = 1` on the instance. fix: when
   `_at_capacity_for_new_key` is true, force `_evict_stale(now)` once and re-test before
   refusing — the map being full is exactly the moment the O(n) scan is worth paying for.

2. [minor] `_SMTP_EXECUTOR` is never shut down — `services/config/email.py:44` — fails when:
   the relay black-holes and the container gets SIGTERM. `concurrent.futures`' `atexit` hook
   joins non-daemon worker threads, so interpreter exit blocks behind a thread still inside
   `smtplib` for up to 4x`_SMTP_TIMEOUT_SECONDS` (~40s), past docker-compose's default 10s
   stop grace. Under `uvicorn --reload` each reload creates a fresh 4-thread pool and leaks
   the old one. fix: `_SMTP_EXECUTOR.shutdown(wait=False, cancel_futures=True)` in app.py's
   `lifespan` teardown.

3. [minor] "revoke it first" can be advice the actor cannot follow — `services/config/invites.py:242`
   — fails when: a superadmin issued a `role="superadmin"` invite scoped to tenant T, it
   expires, and T's tenant_admin re-invites that address. The reclaim correctly declines
   (`may_invite` on the stored role), the INSERT correctly conflicts, and the 409 says
   "revoke it first" — but `POST /invites/{id}/revoke` on that row 403s for the same actor,
   so there is no action the message names that they can take. Rare and not a leak (the row is
   already visible to them in `GET /invites`). fix: no code change needed; if reworded, keep
   it role-blind.

## Verified against the round-1 findings and the coordinator's checklist

- **Sweep latch (was blocking).** `_maybe_sweep(now)` is now the first statement of both
  `over_limit` and `increment`, and `_current` no longer sweeps, so there is no path on which
  the capacity check can skip it. Amortised cost under sustained flood is one O(20,000) scan
  per 500 accesses (~40 ops/access) — the sweep is not the hot path, and it cannot be starved.
- **Cap test (was blocking).** Now loops `_MAX_BUCKETS + 5_000` and asserts `==`: with the cap
  or the `_at_capacity_for_new_key` guards deleted the map lands at 25,000 and the assertion
  fails. `test_limiter_recovers_after_a_flood_once_the_window_passes` asserts `over is False`
  at t=61 after filling to capacity at t=0 — under the round-1 code `over_limit` returned True
  at the capacity check before any sweep, so it fails. Both are genuinely falsifiable; neither
  has an `or` arm, a `!=`, or an absent-value assertion.
- **Lesson 24 not undone.** `routers/tenants.py::list_tenants` is untouched and still scopes on
  `current_user.tenant_id` alone, so Conversation's prewarm and vobiz's telephony lookup keep
  their platform-wide tenant read. The conjunction is added only on `GET /users` and
  `GET /invites`, which have no authority gate beyond the console roles / `require_role`, and
  where a NULL-tenant viewer inheriting the unscoped, `role != 'superadmin'`-unfiltered query
  was a real cross-tenant read. `deps.is_platform_scoped` still answers only "which tenant",
  and the two routes now ask both questions explicitly. `test_viewer_service_account_with_null_
  tenant_cannot_read_other_tenants` creates a real tenant user that the scope-only version
  would return, so it is not vacuous.
- **Dedicated SMTP pool.** Justified rather than over-engineered: it is one module constant and
  one `run_in_executor` call, and it removes a concrete coupling (a stalled relay starving the
  default pool that `authenticate`'s bcrypt now uses). `asyncio.wait_for` still bounds the
  caller — `run_in_executor` returns a `wrap_future`-backed asyncio future whose `cancel()`
  succeeds immediately, so the coroutine returns at the ceiling and AC12 holds (invite stays
  pending and resendable, `email_sent: false`, no 500). With all 4 workers stalled the next
  send sits in the executor queue, has not started, and `wait_for`'s cancel really does remove
  it — no email is sent later out of band. Residual is finding 2 only.
- **Self-heal (parallel change).** `SELECT … FOR UPDATE` on the single pending row the unique
  index guarantees, then reclaim only if `expires_at <= now()` **and** `may_invite` passes on
  the row's *stored* role/tenant — so a tenant_admin cannot clear an invite it could not revoke
  directly, and the tenant COALESCE still fences other tenants out. Reclaim, audit row (with
  the `expired_reclaimed_on_reinvite` marker, satisfying my round-1 finding 3 and design line
  206) and INSERT are all in the one `conn.transaction()`. Concurrent re-invites serialise on
  the row lock; the loser re-reads no pending row, then conflicts on the INSERT and gets the
  409 — correct. `_accesses_since_sweep` rename is consistent with its call sites.
