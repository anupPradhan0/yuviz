> **RESULT UPDATED AT SHIP TIME.** This report was written after T23, before the
> Phase 3/4 review fixes, the STARTTLS work and the tenant-scoping fix. Verified final
> numbers: **286 passed** (`pytest services/config/tests/ -q`), confirmed on two
> consecutive full runs. One pre-existing flake, `test_tenants.py::
> test_concurrent_updates_do_not_produce_stale_audit_old_value`, fails intermittently
> only under full-suite concurrency; it passes 3/3 in isolation, predates this branch
> (test file unmodified since the initial commit), and was observed before this feature
> touched tenants.py. The acceptance-criteria map below remains accurate.

# Test report
COMMAND: POSTGRES_DSN="postgresql://<local-pg-user>@localhost:5432/voiceai" ./venv/bin/python3 -m pytest services/config/tests/ -q
RESULT: 266 passed, 0 failed (265 from the prior pass + 1 new T23 test this pass; the previously-noted
flaky JWT-tamper test did not flake in this run)

## T23 — what was already covered vs. what was actually missing

Before writing anything, `test_invite_routes.py` (Phase 3, HTTP layer) and `test_invites.py` (function
layer) were read end to end against T23's three named scenarios:

1. **Soft-delete re-invite.** Not covered anywhere. `test_users.py::test_soft_delete_user_excluded_from_get_and_authenticate`
   proves soft-delete alone (get/authenticate return None/fail), and the schema-level partial-index
   behavior (two soft-deleted rows sharing an address) is exercised by the schema test plan, but no
   test chains invite → accept → soft-delete → **re-invite the same address** → accept again and
   checks that a second live row is created and the first row's password is retired. This is a real
   gap — added below.
2. **Normalization.** Already covered twice, not duplicated: `test_invites.py::TestCreateInvite::
   test_conflict_check_is_case_insensitive_against_existing_account` (an existing `bob@x.com` account
   blocks an invite to `Bob@X.com`) and `test_invite_stored_and_accepted_email_is_lower_cased_regardless_of_input_case`
   (invite stored from mixed case resolves to one lower-cased user). Both are exactly what T23 asks
   for; a third test at either layer would only restate them.
3. **Audit row per mutating path.** Already covered directly for all four paths, not duplicated:
   `test_success_returns_row_and_raw_token_and_audits` (create), `test_resend_rotates_token_old_404s_new_accepts`
   (resend — includes the redaction check on both `old_value` and `new_value`), `test_revoke_blocks_accept`
   (revoke), `test_creates_user_with_invites_role_tenant_team` (accept) — each queries `audit_log`
   directly by `entity_type`/`action`/`entity_id`, not by reading the source and asserting it calls
   `write_audit`.

**One new test added**, in `services/config/tests/test_invites.py`:
`TestSoftDeleteReinvite::test_reinvite_after_soft_delete_creates_second_live_row_and_retires_old_password`.
Chains `create_invite` → `accept_invite` → `users.soft_delete_user` → `create_invite` (same address) →
`accept_invite` again, then asserts: the second user's id differs from the first; exactly one *live*
row exists for that lower-cased email; the first password no longer authenticates; the second does.

**What would make it fail:** it fails today if `users_email_lower_idx` regresses from the partial
index (`WHERE deleted_at IS NULL`) to a plain unique index on `lower(email)` — the second
`create_invite`/`accept_invite` would raise `EmailConflict`/`EmailTaken` instead of succeeding — or if
`authenticate()`/`get_user_by_email()` stopped filtering `deleted_at`, letting the retired password
keep working or the soft-deleted row block the re-invite.

**Mutation-verified** (both reverted; `git diff services/config/users.py` clean after):
1. Changed `soft_delete_user`'s `UPDATE users SET deleted_at = now()` to `SET deleted_at = NULL`
   (simulating soft-delete silently not taking effect) → test failed exactly as expected, with
   `create_invite` raising `EmailConflict` on the re-invite (the live old row blocks it). Reverted.
2. Changed `get_user_by_email`'s query to drop `AND deleted_at IS NULL` (simulating the lookup no
   longer respecting soft-delete) → test failed the same way, for the same reason (the soft-deleted
   row is found and treated as live). Reverted. Both mutations landed on the same failure path
   (`EmailConflict` before assignment of `row_2`), which is itself informative: this test's assertions
   past that point (`user_2["id"] != user_1["id"]`, the live-count query, the two `authenticate` calls)
   are only exercised once the create/accept sequence actually succeeds — they were not separately
   mutation-tested here because the two most security-relevant failure modes (soft-delete not taking,
   and the lookup ignoring it) both short-circuit at the same earlier point. That earlier point is
   itself the correct thing to be guarding.

Net new tests this batch: 1 (well under the 8-test cap).

## Acceptance criteria coverage map (13 ACs, PRD `01-prd.md`) — full-stack, all four phases built

| AC | Covered by | Status |
|----|-----------|--------|
| 1. super_admin invites tenant_admin into Tenant A → pending invite + email sent | Creation half: `test_invites.py::TestMayInvite` (superadmin path) + `TestCreateInvite`; HTTP half: `test_invite_routes.py::TestCreateAndDelivery::test_create_returns_201_with_email_sent_true` (`email_sent: bool` in body, SMTP mocked to succeed at the `send_invite_email` boundary). **Gap found**: no test exercises `email.py::send_invite_email` itself with `smtplib.SMTP` mocked — every existing test patches `send_invite_email` as a whole, so the actual composed message (token in the URL **fragment**, never a path segment — the specific security property T15's design section calls out) is never asserted against. | Partially covered — invite creation and the `email_sent` flag are covered; the email body/token-placement content is unproven |
| 2. tenant_admin → super_admin rejected | `test_tenant_admin_cannot_invite_superadmin`, `TestCreateInvite::test_denies_privilege_escalation` | Covered |
| 3. tenant_admin → Tenant B rejected regardless of role | `test_tenant_admin_cannot_invite_outside_own_tenant` (parametrized incl. `tenant_id=None`) | Covered |
| 4. tenant_admin → {tenant_admin, supervisor, agent, viewer} in own tenant allowed | `test_tenant_admin_may_invite_own_tenant` (parametrized) | Covered |
| 5. accept creates user with exactly the invite's role/tenant/team | `TestAcceptInvite::test_creates_user_with_invites_role_tenant_team` | Covered |
| 6. invite >7 days old refused at accept; shows expired in the UI | Refusal: `test_expired_invite_raises_invite_expired`. **UI-visible "expired" status is unproven**: `admin-ui/` has no test script, no jest/vitest config, and no test files at all (checked `admin-ui/package.json` — only `dev`/`build`/`start`/`lint`); `admin-ui/app/users/page.tsx` and `admin-ui/app/invite/page.tsx` exist but are not exercised by any automated test. This half can only be proven by a human or a browser-automation harness that does not exist in this repo. | Partially covered — refusal proven; the UI-visible half is unproven, not quietly marked covered |
| 7. resend issues new token, old one no longer accepts | `test_resend_rotates_token_old_404s_new_accepts` | Covered |
| 8. revoke blocks accept; revoking an accepted invite doesn't delete the user | `test_revoke_blocks_accept` (pending-invite revoke, now also asserting the `audit_log` row). `test_revoke_after_accept_does_not_touch_the_created_user` proves the safety property (user untouched) regardless of how `revoke_invite` responds. **Still an open implementation gap, not a test gap**: `revoke_invite` raises `InviteNotPending` on an already-accepted row rather than transitioning it to `revoked` — the transition half of AC8's literal wording is not implemented. | Partially covered — safety property covered; the accepted→revoked transition itself remains an unimplemented/untested gap, carried forward from the prior pass |
| 9. cross-tenant conflict tenant-blind; own-tenant conflict may say so; super_admin conflict names the tenant | Function level: `test_cross_tenant_conflict_is_tenant_blind_and_matches_own_tenant_conflict`, `test_duplicate_pending_invite_same_tenant_is_conflict`, `test_conflict_check_is_case_insensitive_against_existing_account`. HTTP-body wording is exercised indirectly through the 409 status in `test_invite_routes.py`'s throttle tests, but no HTTP-layer test asserts the exact response body text distinguishing the three actor/target combinations the way the function-level tests do. | Covered at function level (where the wording itself is asserted); HTTP-body wording for this specific AC not independently re-asserted at the router layer |
| 10. replayed accepted token rejected as "already used," distinct from expired/revoked | `test_replay_of_accepted_token_raises_invite_used_distinct_from_expired_and_revoked` plus the three `InviteContextGone` tests (a fourth, distinct outcome) | Covered |
| 11. tenant_admin sees only own tenant; super_admin sees all | HTTP/API level, now built and tested: `test_invite_routes.py::TestScoping::test_tenant_id_filter_is_forced_for_tenant_admin` and `test_superadmin_sees_across_tenants_unfiltered` (`GET /invites`); `test_idor_resend_and_revoke_of_another_tenants_invite_is_403` (IDOR on resend/revoke). **UI-visible half unproven**: `admin-ui/app/users/page.tsx` renders this scoping but, per the note under AC6, `admin-ui/` has no test infrastructure — the nav-item-only-for-superadmin/admin and the tenant-scoped table rendering are unverified by any automated test. | Covered at the API layer (was "Phase 3, not testable yet" last pass); UI-rendering half unproven |
| 12. SMTP send failure still shows invite as pending, resendable | Now built and tested: `test_invite_routes.py::TestCreateAndDelivery::test_smtp_failure_is_non_fatal_and_row_stays_pending_and_resendable` (create AND resend both patched to raise, row stays `pending`, second resend succeeds). **UI half unproven** for the same reason as AC6/AC11 — no test asserts the users page actually renders a `email_sent: false` invite as "pending" with a working resend button. | Covered at the API layer (was "Phase 3, not testable yet" last pass); UI-rendering half unproven |
| 13. every invite create/resend/revoke/accept action is audit-logged | All four paths assert directly against `audit_log` rows (see T23 write-up above) | Covered |

FAILURES: None
UNCOVERED (stated explicitly, not marked covered):
- AC1's email body/token-placement content — no test exercises `email.py::send_invite_email` with
  `smtplib.SMTP` mocked; every test patches the function itself away.
- AC6's UI-visible "expired" status, AC11's UI-visible tenant scoping, AC12's UI-visible
  "pending + resendable on send failure" — all three are real screens in `admin-ui/app/users/page.tsx`
  and `admin-ui/app/invite/page.tsx`, but `admin-ui/` has no test script and no jest/vitest
  configuration at all (confirmed via `package.json` and a repo-wide search for `*.test.*`); no test
  infrastructure was built for this report, per instruction, so these UI-only halves are unproven, not
  quietly assumed covered by the API-level tests above.
- AC8's accepted→revoked transition: `revoke_invite` raises `InviteNotPending` on an already-accepted
  invite instead of transitioning it to `revoked`. This is an implementation gap carried forward from
  the prior pass, not something a new test can paper over — the safety property (user left untouched)
  is proven, the state transition itself is not implemented.
