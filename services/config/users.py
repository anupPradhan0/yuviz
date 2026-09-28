"""
User CRUD + authentication. Same audited-mutation pattern as tenants.py/
agents.py — no Redis caching here, unlike those: auth checks are
comparatively low-frequency (once at login, not per hot-path call) and
correctness (a role change or deactivation taking effect immediately)
matters more than shaving a few ms off a login request.

auth.hash_password/verify_password are bcrypt (cost 12, ~250ms of pure CPU)
called synchronously — every call site here goes through asyncio.to_thread
so that work runs off the event loop (lesson 18), not just the ones already
reachable from an authenticated route: POST /invites/accept
(invites.accept_invite -> _insert_user) is public and unauthenticated, so
without this a burst of accepts at the AcceptThrottle ceiling would stall
the whole loop, including /health.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import audit, auth, db

_UPDATABLE_FIELDS = {"role", "tenant_id"}

_SUPERADMIN_EXISTS = (
    "SELECT EXISTS (SELECT 1 FROM users WHERE role = 'superadmin' AND deleted_at IS NULL)"
)

_BOOTSTRAP_LOCK_KEY = 7749012026


def to_public_dict(user: dict[str, Any]) -> dict[str, Any]:
    """Strips password_hash — never returned to a client, in a login
    response or anywhere else."""
    return {k: v for k, v in user.items() if k != "password_hash"}


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    """Pre-auth (login): there is no tenant to scope this read to yet, and
    the row may be a NULL-tenant platform account — platform_conn bypass."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-login") as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE lower(email) = lower($1) AND deleted_at IS NULL", email,
        )
    return dict(row) if row is not None else None


async def get_user_by_id(user_id: Any) -> dict[str, Any] | None:
    """Identity resolution ONLY — answers "who is the actor on this
    request", not "may this actor touch that user". Shared by exactly four
    callers (deps.assert_current_authority, deps.fresh_authority,
    routers/auth.py's GET /auth/me, routers/tenants.py's concurrency route),
    all of which run before the request's tenant is known and all of which
    must be able to see a NULL-tenant platform account, which no RLS policy
    can ever return — hence the one platform_conn bypass here, not per
    caller. See get_user_for_admin() below for the "may this actor touch
    that user" question, which takes the normal tenant-scoped path."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="identity-resolution") as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1 AND deleted_at IS NULL", user_id,
        )
    return dict(row) if row is not None else None


async def get_user_for_admin(user_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    """"May this actor touch THAT user" — the Tier 3 by-id resolver for
    PATCH/DELETE /users/{user_id}, as opposed to get_user_by_id's identity
    resolution. `platform_scoped` (source: deps.is_platform_scoped(current_user)
    only, lesson 24) selects platform_conn for a platform actor's read;
    every other caller takes the ambient tenant_conn() ceiling, so a
    tenant-scoped actor asking for another tenant's user id gets today's
    404 from an empty policy result (lesson 2)."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="users-admin-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1 AND deleted_at IS NULL", user_id,
        )
    return dict(row) if row is not None else None


async def list_users(*, tenant_id: Any | None, is_platform_scoped: bool) -> list[dict[str, Any]]:
    # Service accounts (conversation-service@internal.yuviz.ai etc.) never
    # appear here — see is_service_account's schema.sql comment for why:
    # an admin soft-deleted one through this exact listing once already,
    # breaking every live call until it was noticed. They're managed
    # directly in Postgres, not through the Users UI.
    #
    # `is_platform_scoped` reflects the *actor's* tenant scope
    # (deps.is_platform_scoped: tenant_id is None), not their role — a
    # platform-scoped actor (superadmin, or a viewer-role service account
    # like Conversation/vobiz, lesson 24) filtering to one tenant via
    # ?tenant_id= still gets every role in that tenant; only a tenant-
    # scoped actor gets the `role != 'superadmin'` exclusion, since they
    # must never see a superadmin row regardless of tenant. PR #19 finding
    # 4: this used to be `role == "superadmin"`, which scoped a NULL-tenant
    # service account to `tenant_id IS NOT DISTINCT FROM NULL` instead of
    # the platform-wide access it actually needs. (Also review finding 3,
    # earlier in the same PR: `is_superadmin`/tenant_id were conflated,
    # which silently dropped superadmin-role rows from a superadmin's own
    # filtered listing — same fix, still holds under the new name.)
    # A NULL tenant_id argument is, by construction, a listing no RLS policy
    # can ever return (platform-scoped cross-tenant, or a NULL-tenant
    # service account's own scoped view) — platform_conn regardless of
    # is_platform_scoped, so a naive tenant_conn() here doesn't raise
    # TenantUnresolved on a request that isn't actually an error (the
    # NULL-tenant, non-superadmin service-account case; see "GET /users for
    # a NULL-tenant service account" in the design doc). Every other
    # listing is scoped to a real tenant_id and takes tenant_conn().
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="users-null-tenant-listing") if tenant_id is None else tenant_conn(pool)
    async with conn_cm as conn:
        if is_platform_scoped and tenant_id is None:
            rows = await conn.fetch(
                "SELECT * FROM users WHERE deleted_at IS NULL AND NOT is_service_account ORDER BY email",
            )
        elif is_platform_scoped:
            rows = await conn.fetch(
                "SELECT * FROM users WHERE tenant_id IS NOT DISTINCT FROM $1 "
                "AND deleted_at IS NULL AND NOT is_service_account ORDER BY email",
                tenant_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM users WHERE tenant_id IS NOT DISTINCT FROM $1 "
                "AND role != 'superadmin' AND deleted_at IS NULL AND NOT is_service_account ORDER BY email",
                tenant_id,
            )
    return [dict(row) for row in rows]


async def _insert_user(
    conn: Any,
    *,
    email: str,
    password: str,
    role: str,
    tenant_id: Any | None,
    creator_user_id: Any | None,
    creator_user_email: str | None,
    team: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    phone: str | None = None,
    signup_source: str | None = None,
) -> dict[str, Any]:
    """Insert + audit on a caller-supplied connection, already inside a
    transaction — so seed_superadmin()/register_admin() can share their
    transaction, which create_user()'s own connection could not."""
    row = await conn.fetchrow(
        "INSERT INTO users (email, password_hash, role, tenant_id, team, "
        "first_name, last_name, phone, signup_source) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING *",
        email.lower(), await asyncio.to_thread(auth.hash_password, password), role, tenant_id, team,
        first_name, last_name, phone, signup_source,
    )
    result = dict(row)
    await audit.write_audit(
        conn,
        entity_type="user",
        entity_id=result["id"],
        action="created",
        user_id=creator_user_id,
        user_email=creator_user_email,
        new_value=result,
    )
    return result


async def create_user(
    *,
    email: str,
    password: str,
    role: str = "admin",
    tenant_id: Any | None = None,
    creator_user_id: Any | None = None,
    creator_user_email: str | None = None,
) -> dict[str, Any]:
    # tenant_id may be a real tenant or NULL (a platform admin) — either
    # way there's no request-scope GUC to inherit here (not called from a
    # tenant-path route today), so this bypasses like the other pre-auth/
    # admin-provisioning writers in this module.
    pool = await db.get_pool()
    async with platform_conn(pool, reason="users-create", stamp_tenant=tenant_id) as conn:
        return await _insert_user(
            conn,
            email=email,
            password=password,
            role=role,
            tenant_id=tenant_id,
            creator_user_id=creator_user_id,
            creator_user_email=creator_user_email,
        )


async def superadmin_exists() -> bool:
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-bootstrap") as conn:
        return await conn.fetchval(_SUPERADMIN_EXISTS)


async def seed_superadmin(*, email: str, password: str) -> dict[str, Any] | None:
    """Creates the platform superadmin unless one already exists (returns
    None), so restarts never duplicate it or reset changed credentials.
    Advisory-locked so concurrent seeds can't both insert."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-bootstrap") as conn:
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _BOOTSTRAP_LOCK_KEY)
        if await conn.fetchval(_SUPERADMIN_EXISTS):
            return None
        return await _insert_user(
            conn,
            email=email,
            password=password,
            role="superadmin",
            tenant_id=None,
            creator_user_id=None,
            creator_user_email=email,
        )


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "org"


async def register_admin(
    *,
    email: str,
    password: str,
    organization_name: str,
    first_name: str | None,
    last_name: str | None,
    phone: str | None = None,
    signup_source: str | None = None,
) -> dict[str, Any]:
    """Public signup: a new tenant plus its first admin, in one transaction.
    The role is fixed here — no caller input can make this a superadmin.
    Raises asyncpg.UniqueViolationError if the email is taken."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-register") as conn:
        slug = _slugify(organization_name)
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM tenants WHERE slug = $1)", slug):
            slug = f"{slug}-{secrets.token_hex(3)}"
        tenant = dict(await conn.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", organization_name, slug,
        ))
        await audit.write_audit(
            conn, entity_type="tenant", entity_id=tenant["id"], action="created",
            user_id=None, user_email=email.lower(), new_value=tenant,
        )
        return await _insert_user(
            conn,
            email=email,
            password=password,
            role="admin",
            tenant_id=tenant["id"],
            creator_user_id=None,
            creator_user_email=email.lower(),
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            signup_source=signup_source,
        )


async def authenticate(email: str, password: str) -> dict[str, Any] | None:
    """Returns the user row on success, None on any failure (unknown email,
    wrong password) — deliberately the same shape for both, so a login
    endpoint can't be used to enumerate which emails have accounts."""
    user = await get_user_by_email(email)
    if user is None:
        return None
    if not await asyncio.to_thread(auth.verify_password, password, user["password_hash"]):
        return None
    return user


async def update_user(
    user_id: Any,
    *,
    actor_user_id: Any | None = None,
    actor_user_email: str | None = None,
    row_tenant_id: Any | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """`row_tenant_id` is the target row's tenant_id, already fetched and
    authorized by the router (get_user_for_admin + assert_tenant_access) —
    passed through rather than re-derived so the fetch and this mutation
    share one resolved scope. None means a genuinely platform-scoped row
    (no tenant to write under RLS), so that branch alone bypasses; every
    real-tenant row mutates on plain tenant_conn(), which the router has
    already pointed at this tenant via set_target_tenant()."""
    if not fields:
        raise ValueError("update_user() called with no fields to update")

    new_password = fields.pop("password", None)

    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_user() got non-updatable field(s): {unknown}")

    if new_password is not None:
        fields["password_hash"] = await asyncio.to_thread(auth.hash_password, new_password)

    if not fields:
        raise ValueError("update_user() called with no fields to update")

    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="users-admin-by-id", stamp_tenant=row_tenant_id)
        if row_tenant_id is None
        else tenant_conn(pool)
    )
    async with conn_cm as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1 FOR UPDATE", user_id,
        )
        if old_row is None:
            raise LookupError(f"user {user_id} not found")
        old = dict(old_row)
        if old["is_service_account"]:
            raise ValueError(
                "cannot update a service account through this endpoint — "
                "manage it directly in Postgres",
            )

        columns = list(fields.keys())
        set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(columns))
        new_row = await conn.fetchrow(
            f"UPDATE users SET {set_clause}, updated_at = now() WHERE id = $1 RETURNING *",
            user_id, *(fields[col] for col in columns),
        )
        new = dict(new_row)

        # old_value/new_value cover only the columns actually written, not
        # the full row — otherwise password_hash (redacted either way)
        # rides along on every role/tenant_id-only update and the UI
        # can't tell "redacted, unchanged" from "redacted, changed."
        await audit.write_audit(
            conn,
            entity_type="user",
            entity_id=user_id,
            action="updated",
            user_id=actor_user_id,
            user_email=actor_user_email,
            old_value={col: old[col] for col in columns},
            new_value={col: new[col] for col in columns},
        )
    return new


async def change_password(
    user_id: Any, *, current_password: str, new_password: str, platform_scoped: bool = False,
) -> bool:
    """Returns False (no write happens) if current_password doesn't match —
    the router turns that into a 400, distinct from update_user()'s
    role/tenant_id path since this always needs the caller to prove they
    still know the old password, not just be authenticated as someone with
    permission to edit the row.

    Always the caller's own row (user_id is current_user.id), so
    `platform_scoped` is the caller's own known tenant_id-is-None-ness
    (deps.is_platform_scoped(current_user)) — no fetch-then-decide needed,
    unlike the admin-by-id routes."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="users-change-password") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1 FOR UPDATE", user_id)
        if row is None:
            raise LookupError(f"user {user_id} not found")
        user = dict(row)
        if not await asyncio.to_thread(auth.verify_password, current_password, user["password_hash"]):
            return False

        new_hash = await asyncio.to_thread(auth.hash_password, new_password)
        await conn.execute(
            "UPDATE users SET password_hash = $2, updated_at = now() WHERE id = $1",
            user_id, new_hash,
        )
        # old_value/new_value both carry password_hash, but audit.py
        # redacts that field before it ever reaches Postgres (see
        # audit.py's _SECRET_REF_FIELDS) — this row only records "a
        # password change happened," never either hash.
        await audit.write_audit(
            conn,
            entity_type="user",
            entity_id=user_id,
            action="updated",
            user_id=user_id,
            user_email=user["email"],
            old_value={"password_hash": user["password_hash"]},
            new_value={"password_hash": new_hash},
        )
    return True


async def change_email(
    user_id: Any, *, current_password: str, new_email: str, platform_scoped: bool = False,
) -> dict[str, Any] | None:
    """Caller's own email, gated on their current password like
    change_password(). Returns None if the password is wrong; raises
    asyncpg.UniqueViolationError if the address is taken."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="users-change-password") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1 FOR UPDATE", user_id)
        if row is None:
            raise LookupError(f"user {user_id} not found")
        user = dict(row)
        if not await asyncio.to_thread(auth.verify_password, current_password, user["password_hash"]):
            return None
        new_row = await conn.fetchrow(
            "UPDATE users SET email = $2, updated_at = now() WHERE id = $1 RETURNING *",
            user_id, new_email.lower(),
        )
        await audit.write_audit(
            conn,
            entity_type="user",
            entity_id=user_id,
            action="updated",
            user_id=user_id,
            user_email=user["email"],
            old_value={"email": user["email"]},
            new_value={"email": new_row["email"]},
        )
    return dict(new_row)


async def soft_delete_user(
    user_id: Any,
    *,
    actor_user_id: Any | None = None,
    actor_user_email: str | None = None,
    row_tenant_id: Any | None = None,
) -> None:
    """`row_tenant_id` — see update_user()'s docstring: the router's own
    fetch (get_user_for_admin + assert_tenant_access) decides it, passed
    through so fetch and delete share one resolved scope."""
    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="users-admin-by-id", stamp_tenant=row_tenant_id)
        if row_tenant_id is None
        else tenant_conn(pool)
    )
    async with conn_cm as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1 FOR UPDATE", user_id,
        )
        if old_row is None:
            raise LookupError(f"user {user_id} not found")
        old = dict(old_row)
        if old["is_service_account"]:
            raise ValueError(
                "cannot deactivate a service account — the platform depends on it "
                "staying active; manage it directly in Postgres if truly needed",
            )

        await conn.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user_id)
        await audit.write_audit(
            conn,
            entity_type="user",
            entity_id=user_id,
            action="deleted",
            user_id=actor_user_id,
            user_email=actor_user_email,
            old_value=old,
        )
