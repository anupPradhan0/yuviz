# Code review — round 2 (final)
VERDICT: GREEN

Scope: `admin-ui/` only. Findings 3 (clock-derived cooldown) and 6 (duplicated
standalone-route guard) from round 1 are accepted as reported and not re-raised.

## Round-1 fixes verified

1. **Expired-invite Resend (was blocking) — fixed, correct in both directions.**
   `app/users/page.tsx:280-281`: `revocable = inv.status === "pending"` (raw),
   `resendable = deriveStatus(inv) === "pending"` (derived). Traced all four states:
   pending+live → Resend + Revoke; pending+expired → Revoke only (server allows it,
   it checks stored status only); revoked → neither, renders `—`; accepted → neither,
   renders `—`. The product decision holds: no status falls through to showing both.
   `deriveStatus` can only *narrow* pending, so `resendable ⊆ revocable` — there is no
   state that offers Resend without Revoke, and the `!resendable && !revocable` arm is
   reachable exactly for revoked/accepted.

2. **SMTP notice (was blocking) — fixed.** `notice` is page-level state rendered at
   `app/users/page.tsx:197-201`, outside the `<Modal>` and outside any modal-conditional
   subtree, next to the `error` banner. `setModalOpen(false)` before `setNotice(...)` in
   the same handler is fine — React batches both into one re-render and the banner does
   not depend on `modalOpen`, so the notice paints on the same commit that closes the
   modal.

3. **Role widening (was minor) — fixed, no stragglers.** `lib/api.ts:724` now carries the
   five roles in `users_role_check`; `InviteRole` is an alias, so the `as InviteRole`
   cast is gone (`app/users/page.tsx:234` is now a plain `ROLE_BADGE[u.role]`). Both
   `Record<UserRole, string>` maps in `settings/page.tsx` (`:80` badge map, `:274` edit
   select) list all five — and being `Record<UserRole, …>`, `tsc` now fails if a sixth
   role is added without updating them. Grepped every `role ===` / role map under
   `admin-ui/`: the rest (`agents/*`, `ProvidersPanel`, `LocalVoicePicker`) are provider
   roles (`stt`/`llm`/`tts`), unrelated. `lib/auth.ts:9` still declares a three-role
   `StoredUser`, but that interface has zero references anywhere in the app — dead,
   pre-existing, outside this diff.

4. **Invite button gating (was minor) — fixed.** `app/users/page.tsx:188` gates on
   `canManageUsers = isSuperadmin || currentUser?.role === "admin"`, the same shape as
   `settings/page.tsx:103`'s `canCreate` and `AppShell.tsx:145`.

**Settings page (edited for an unrelated reason) — re-checked, clean.** The only changes
are the two widened role lists; `UserRole` is still imported and used, `Modal` is still
used by the surviving edit/delete modals, `canCreate` still has its one call site, and
nothing references the removed create-user state. No dead code left behind.

## Remaining

1. [minor] `notice` is never cleared except by re-opening the invite modal —
   `app/users/page.tsx:197` — fails when: an invite's email fails to send (banner
   appears), the operator fixes SMTP and clicks Resend on that row successfully; the
   banner still reads "the email failed to send", with no dismiss control, until the
   page is navigated away from or the invite modal is reopened. Fix: `setNotice(null)`
   alongside the existing `setError(null)` in `handleResend`/`handleRevoke`, or add a
   dismiss affordance. Not blocking — the message is stale, never wrong about a
   different invite.
