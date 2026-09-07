> **SUPERSEDED — this file records review round 1 only.**
> Both blocking findings below were fixed and re-verified GREEN in rounds 2 and 3:
> cross-tenant membership was moved out of scope with the `schema.sql:223` UNIQUE-email
> constraint cited in Constraints, and AC10 was added for the already-used token state.
> The PRD's final verdict is **GREEN**. This file was never overwritten by the later rounds.

\# Review: 01-prd.md (Invite-based user onboarding across the platform/tenant hierarchy)
VERDICT: RED

1. [blocking] The multi-tenant-membership requirement (Scope: "adds a second membership without forking identity"; AC5, AC9) is physically impossible against the current schema — `database/schema.sql:223` has `email TEXT NOT NULL UNIQUE` and `users.tenant_id` is a single scalar column (`schema.sql:220-229`), so one email cannot hold two tenant rows today. The Constraints section only mentions extending the `role` CHECK via `ALTER TABLE` — it never mentions relaxing the email-uniqueness invariant or adding a membership/join table. — fix: either drop cross-tenant-attach from scope, or add an explicit constraint/schema change (e.g., a `user_tenant_memberships` table or a composite unique(email, tenant_id)) to the PRD's Constraints section so the design stage doesn't discover this mid-build.

2. [blocking] AC5 has no case for a token that has already been used to accept — nothing says a second visit to the same accept link (with the same token) is rejected. Combined with finding 1's ambiguity about what "gains a new tenant membership" means in a UNIQUE-email schema, a replayed accept link could silently reset a live user's password or attempt a duplicate insert that hits the unique constraint uncontrolled by any stated behavior. — fix: add an AC: "Given an already-accepted invite token, when it is reused, then acceptance is refused with an 'already used' state distinct from expired/revoked."

3. [blocking] No acceptance criterion covers inviting an email that already has an account in the *same* tenant (only AC9 covers a different tenant). A tenant_admin re-inviting an existing member of their own tenant has undefined behavior — could silently create a duplicate pending invite, or (per finding 1) collide on the UNIQUE email constraint. — fix: add an AC: "Given an email that is already an active member of Tenant A, when Tenant A's admin invites that same email, then the request is rejected (or offered as a role-change flow), not silently queued as a fresh invite."

4. [minor] Open question 1 ("can tenant_admin mint a tenant_admin peer?") is left open while Scope line 12 and AC4 already assert tenant_admin *can* invite tenant_admin as settled scope — the scope statement and the open question contradict each other. This is a privilege-escalation-relevant decision (a tenant_admin minting equal-privilege peers unbounded) and should not ship as both "in scope" and "unresolved." — fix: resolve open question 1 before hand-off; if unresolved, mark AC4's tenant_admin→tenant_admin invite as provisional/excluded from Scope rather than asserted as in.

5. [minor] No case for a role or team change to a pending (not-yet-accepted) invite — e.g., an admin invites someone as `agent` then needs to correct it to `supervisor` before they accept. Today's only stated remedies are revoke+re-invite (new token, new email) or resend (same role/team, new token only). — fix: either explicitly state "role/team is immutable once sent; correcting it requires revoke + new invite" as a constraint, or add an AC for in-place role edit on a pending invite.
