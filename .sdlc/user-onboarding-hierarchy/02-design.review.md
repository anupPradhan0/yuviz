# Review: 02-design.md
VERDICT: AMBER

1. [minor] Resend/revoke load the invite row before calling `may_invite`, so a cross-tenant or super_admin invite id now correctly returns 403 (the IDOR is closed) — but this ordering means a tenant_admin can still distinguish "invite id exists (any tenant)" via 403 from "invite id does not exist" via 404, a minor existence-enumeration side-channel across tenant boundaries — Interfaces "Permission contract" — fix: return 404 (not 403) when the loaded invite's tenant_id != actor's tenant_id and actor is not super_admin, so cross-tenant ids are indistinguishable from nonexistent ones (low practical risk since ids are UUIDs, but worth a one-line fix given the effort already spent closing the IDOR).
