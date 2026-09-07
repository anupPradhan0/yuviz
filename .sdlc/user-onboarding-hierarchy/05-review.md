# Code review (round 2 — re-review of four fixes)
VERDICT: AMBER

Round-1 findings 1-4 are fixed and verified below. Findings 5 and 6 (unreachable `_iter_api_routes`
arm, untracked CSV/XLSX) are accepted as reported and not re-raised. One new, lower-severity
consequence of the shell fix remains.

1. [minor] `start_data`'s new nonzero `return` kills the operator's shell on the *benign* first-run
   path — `scripts/start_local.sh:41-46` — fails when: a new contributor follows `docs/setup.md:158`,
   runs `source scripts/start_local.sh; start_data` before `createdb voiceai`. psql exits 2, the
   "voiceai db missing — run: psql postgres -c 'CREATE DATABASE voiceai;'" message is written to
   stderr, then `return 1` propagates into the `set -euo pipefail` that sourcing installed in the
   interactive shell and the shell exits — taking the terminal tab (and its message) with it on any
   close-on-exit profile. Verified empirically here, not assumed: `bash -i -c 'source f; f'` and
   `zsh -i -c 'source f; f'` with `set -euo pipefail` and a function returning 1 both terminate
   before the next command runs; with `return 0` both survive. This is new — before this diff no path
   in `start_data` could return nonzero. The failure it was added to surface (rc=3) is worth stopping
   for; rc=2 on a fresh checkout is not. Fix: `return 0` after the "db missing" message (keep the
   `return "$schema_rc"` for every other nonzero), or drop `set -e` from a file whose documented
   usage is `source`.

## Verified, with the mutation actually traced

**Fix 1 — exit-code branch (scrutinised hardest).** The branch is empirically correct, not just
documented: on this machine `psql <missing-db>` exits **2** and a `DO $$ ... RAISE EXCEPTION` under
`-v ON_ERROR_STOP=1` exits **3**, with the statement after the RAISE confirmed not applied. Anything
nonzero other than 2 now prints psql's real stderr (no `2>/dev/null` anywhere on this path) and
returns `$schema_rc`. No `|| true` or discarded status remains on the `schema.sql` line; the
surviving `|| true` is on `knowledge_schema.sql`, which was never in T6b's scope and was already
that way. `psql ... || schema_rc=$?` is `set -e`-safe (the `||` protects it) and `set -u`-safe
(`schema_rc` is initialised). `local schema_rc` is valid in both bash and zsh, and the zsh
read-only-`status` hazard the implementer caught is genuinely gone — I ran the exact
`local schema_rc=0; false || schema_rc=$?` shape under `zsh -i` and got `rc=1`. Nothing else in the
repo calls `start_data` (grepped `scripts/`, `docs/`): both launchers define their own copy and the
operator invokes it by hand, so there is no caller discarding the new return value. The sibling
`scripts/start_web_test.sh` needs no equivalent change — its schema line has no fallback at all and
runs under `set -e`, so a failure already stops it before the "✓ schema applied" echo.

**Fix 2 — console gate test** (`services/config/tests/test_console_gate.py:114-141`) genuinely kills
the mutation. It now inserts a real `supervisor`/`agent` user via `create_user`, signs a token for
that row, and asserts `GET /auth/me` == 200 with the right email and `POST /auth/change-password` ==
204 using the real current password. Reverting either route to `Depends(get_current_user)` makes the
gate return 403 and both asserts fail — and unlike the old `!= 403`, a deleted route (404) or a
missing user row (401) now fails too.

**Fix 3 — cross-tenant conflict test** (`test_invites.py:161-224`) constructs the case that was
missing: the colliding account lives in a genuinely different tenant from both the actor and the
invite's `tenant_id`. Deleting `_conflict_tenant_name`'s `actor.role != "superadmin"` guard makes
`tenant_name` return `other["slug"]`, failing `assert cross_tenant_exc.value.tenant_name is None`.
The fold dropped nothing: the old own-tenant tenant-blind case survives as the `own_tenant_exc`
block, and the old superadmin case is strictly strengthened — it previously asserted the slug of a
tenant that was *both* the invite target and the existing user's tenant, so it could not tell the
two apart; the new one asserts `other["slug"]`, which fails if the code ever names the requested
tenant instead of the existing account's.

**Fix 4 — resend redaction** (`test_invites.py:281-297`) asserts the retired hash is absent from
`old_value`, the new hash absent from `new_value`, and `[redacted]` present in both. Removing
`token_hash` from `audit.py`'s `_SECRET_REF_FIELDS` puts the raw hashes straight into the JSON and
fails the first two asserts — a real kill, and it covers the rotation path that is the only one
writing the secret into `old_value`.
