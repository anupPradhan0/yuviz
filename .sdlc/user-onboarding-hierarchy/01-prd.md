# PRD: Invite-based user onboarding across the platform/tenant hierarchy

## Problem
Today `services/config/routers/users.py` lets an existing admin create a user by typing an email and a temporary password directly — there is no invite, no email delivery, and no way for a new hire to set their own credential. There are also only three roles (`superadmin`, `admin`, `viewer`); nothing distinguishes a call-center supervisor from an agent, and no admin-ui screen lists or manages users at all. Every new hire today is either handed a password out-of-band or never gets an account, and there is no self-service path for a tenant to grow its own staff.

## Hierarchy decision
**Two isolation boundaries: Platform and Tenant. No Org layer, no Call Center boundary.** Tenant is the single scoping key already on every table (`tenant_id`); adding an Org layer above it would double every permission check and audit path in every query for a multi-entity customer we don't have today, and no CCaaS competitor researched (Genesys, Flex, Connect) ships one. Call Center becomes a **team**, an attribute on a tenant-scoped user, not an isolation boundary — a hard boundary would forbid a supervisor from covering more than one call center, which the business needs to allow.

Roles: the stored `role` value `superadmin` in `database/schema.sql` is kept exactly as-is — **zero migration** — and is displayed in the UI and in this and future docs as **super_admin**; this is a naming/display change only, not a schema change. super_admin is the platform operator (us): sees every tenant's data, billing, and plan, and manages tenants — that visibility is real today or planned elsewhere, and is out of scope for this build (see Scope). `admin` becomes **tenant_admin**, unchanged in storage, tenant-scoped, and manages their own tenant's users. Two new tenant-scoped roles are added: **supervisor**, who joins a live call mid-conversation to coach the agent, and **agent**, a human who takes over a call the AI could not resolve. The existing **viewer** role is kept as the fourth tenant-scoped role, unchanged in meaning (read-only). A tenant_admin may invite a peer tenant_admin, but only within their own tenant — never super_admin, and never into another tenant.

## Scope
- In: super_admin can invite tenant_admin (any tenant) and super_admin.
- In: tenant_admin can invite tenant_admin, supervisor, agent, viewer — only within their own tenant.
- In: the full invite lifecycle — create, SMTP email delivery, accept (sets password, creates the user), resend (reissues the token, invalidates the prior one), revoke, and expiry (7 days, matching the researched competitor norm). Role and team are fixed at send time and cannot be edited on a pending invite; changing either requires revoking and sending a new invite.
- In: an admin-ui user-management screen scoped per tenant, plus a platform-wide equivalent for super_admin, listing users and pending invites with resend/revoke actions and visible invite status (pending, expired, revoked, accepted).
- In: every invite create, resend, revoke, and accept is recorded in the audit trail with actor, action, and old/new value, matching the pattern already used across this repo.
- Out: reusable/open invite links and domain auto-join — rejected by the competitor research on PII and seat-billing grounds.
- Out: an Org layer above Tenant, or any cross-tenant reporting group.
- Out: cross-tenant membership for one email. `users.email` is `UNIQUE` with a single scalar `tenant_id` (`database/schema.sql:223`), so one account belongs to exactly one tenant today; an invite sent to an email that already has an account anywhere on the platform is rejected, not merged, and no schema change to support multi-tenant membership is part of this build.
- Out: SSO/SAML-based provisioning.
- Out: self-serve signup with no invite — every account originates from an invite.
- Out: billing and plan management screens. super_admin's ability to see tenant billing/plan is described above as part of the role's identity, but no billing or plan UI or API is built as part of this PRD.

## Acceptance criteria
1. Given a super_admin, when they invite a new email as tenant_admin for Tenant A, then a pending invite scoped to Tenant A is created and an email is sent to that address.
2. Given a tenant_admin of Tenant A, when they attempt to invite a super_admin, then the request is rejected.
3. Given a tenant_admin of Tenant A, when they attempt to send an invite scoped to Tenant B, then the request is rejected regardless of the role requested.
4. Given a tenant_admin of Tenant A, when they invite supervisor, agent, viewer, or tenant_admin within Tenant A, then the invite is created scoped to Tenant A.
5. Given a valid, unexpired, unused invite token, when the recipient opens the accept link and sets a password, then a user is created with exactly the role and tenant fixed at invite time.
6. Given an invite older than 7 days, when the recipient opens the accept link, then acceptance is refused and the invite shows as expired in the user-management screen.
7. Given a pending invite, when an authorized admin clicks resend, then a new token is issued and the previous token no longer accepts.
8. Given a pending or accepted invite, when an authorized admin clicks revoke, then the token can no longer be used to accept (revoking an accepted invite does not delete the resulting user).
9. Given a tenant_admin sends an invite to an email that already has an account in a *different* tenant, then the invite is rejected with a generic message that does not name the other tenant or reveal any attribute of the existing account. Given a tenant_admin sends an invite to an email that is already an active member of *their own* tenant, then the rejection message may say so, since the admin is already entitled to know that. Given a super_admin sends an invite to an email with an existing account anywhere, then the response may name the tenant that account belongs to.
10. Given an invite token that has already been accepted once, when it is submitted again, then acceptance is rejected as "already used," distinct from the expired and revoked states.
11. Given a tenant_admin viewing the user-management screen, when the page loads, then only users and invites belonging to their own tenant are visible; given a super_admin viewing the platform-wide screen, then users and invites across all tenants are visible.
12. Given an invite whose SMTP send fails, when an admin views the user-management screen, then the invite still appears as pending and can be resent.
13. Given any invite create, resend, revoke, or accept action, when it completes, then it is recorded in the audit log with actor, action, and old/new value.

## Constraints
- Identity and role come only from the verified JWT (`services/config/auth.py`, `deps.py`); never trust a client-supplied tenant_id or role.
- Reuse `require_role()` in `deps.py` for endpoint-level checks; add the finer-grained self/cross-tenant checks inline as `services/config/routers/users.py` already does for `create_user`.
- New role values (`supervisor`, `agent`) extend the existing `role` CHECK constraint in `database/schema.sql` via an idempotent `ALTER TABLE`, per repo convention — no parallel migration tool. `superadmin` remains the stored string; `super_admin` is display-only.
- `users.email UNIQUE` and its single scalar `tenant_id` stay as-is; invite send must check for an existing account by email and reject per AC9, never assume or create multi-tenant membership.
- An error or rejection message must never reveal the existence or any attribute (tenant, role, status) of a resource belonging to another tenant to a caller not entitled to see it — this is the general rule AC9 is one instance of, and it applies to every cross-tenant lookup this feature introduces (invite conflict checks, resend/revoke on another tenant's invite, etc.).
- Passwords stay bcrypt-hashed; no plaintext temp passwords in the new accept flow (a behavior change from today's `create_user`).
- Mutating actions write to `audit_log` in the same transaction as the mutation, matching every other service in this repo.
- SMTP credentials for invite delivery are configured via environment/secret reference, following this repo's existing `enc:`/`k8s:`/`env:` secret convention (`libs/config_sdk/secrets.py`) — never plaintext in Postgres or in committed config.
- Admin UI work follows `admin-ui/AGENTS.md`; no existing user-management screen exists to extend — this is net-new.

## Open questions
None.
