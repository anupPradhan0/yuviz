# Research: user hierarchy & invite onboarding

## How competitors structure hierarchy
- **Genesys Cloud**: the org is the sole isolation boundary; **divisions** partition queues/users/flows, one per object, roles span several.
- **Twilio Flex**: the account is the boundary (Flex can't attach to an existing account); one TaskRouter Workspace per account holds Workers and TaskQueues.
- **Amazon Connect**: the instance is the container (users, routing profiles, queues); the AWS account bills.
- **Five9 / Talkdesk**: unverified.
- **Okta**: orgs are hard boundaries; groups are grouping only.
- **Slack**: workspace = boundary; **Enterprise Grid** is a real org-above-tenant layer, but Slack advises a channel instead.
- **Stripe**: the account is the data and billing boundary; orgs only grant roles across accounts.

## Invite onboarding: the common pattern
- A record (email, role, inviter, status) plus an opaque single-use token; Okta's are explicitly one-time.
- Short expiry: Okta 7 days, Stripe 10, Slack 30; expired ones purged (Auth0).
- Email-bound: Auth0 requires the IdP to return the invited address, and marks it verified.
- Role is chosen at invite time (Stripe requires one before sending).
- Resend and revoke are first-class admin actions (Auth0, Okta, Slack); resend issues a new token.
- Existing user: log in, then attach membership — Stripe prompts account switching; Auth0 joins the identity.

## Where products differ
- **Reusable links**: Slack allows one link for 400 people — reach vs no audit trail.
- **Domain auto-join**: Slack admits approved domains — less friction vs self-provisioned seats.
- **A real org layer**: only Slack Grid and Stripe orgs — cross-tenant admin vs a second permission model.
- **Identity on accept**: Auth0/Okta create it; Stripe/Slack attach an existing one.

## Recommendation for a CCaaS platform
- **Two isolation boundaries: Platform and Tenant** — what Genesys (org + divisions), Flex (account + queues) and Connect (instance + routing profiles) ship.
- **Cut Org.** No CCaaS product has it; Slack Grid and Stripe orgs serve multi-entity enterprises. It doubles the permission model in every query, role check and audit path — for a customer you don't have.
- **Cut Call Center as a boundary** — make it a team/queue inside the tenant, the analogue of a Genesys division or TaskRouter TaskQueue. Supervisors spanning two call centers must stay possible; a boundary forbids that.
- **Tenant owns data and billing**: one isolation key on every table, one subscription, one admin. Separate billing or data means a second tenant.
- **Roles are tenant-scoped**: platform_admin (staff only), tenant_admin, supervisor, agent — supervisors scoped by queue, as Genesys does with division-aware roles.
- **Invites**: single-use opaque token, email-bound, role and team fixed at invite time, 7-day expiry, resend and revoke. Accepting on an existing email adds a membership, never forks an account.
- **No open links or domain auto-join at launch** — contact centers are PII-heavy and seats billable; both erase who authorised a seat.
- **If cross-tenant reporting is needed later**, add a read-only tenant group above Tenant rather than retrofit an Org into the isolation key.
