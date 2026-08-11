"""
User CRUD + authentication. Same audited-mutation pattern as tenants.py/
agents.py — no Redis caching here, unlike those: auth checks are
comparatively low-frequency (once at login, not per hot-path call) and
correctness (a role change or deactivation taking effect immediately)
matters more than shaving a few ms off a login request.
"""

from __future__ import annotations

from typing import Any

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
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM users WHERE email = $1 AND deleted_at IS NULL", email,
    )
    return dict(row) if row is not None else None


async def get_user_by_id(user_id: Any) -> dict[str, Any] | None:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM users WHERE id = $1 AND deleted_at IS NULL", user_id,
    )
    return dict(row) if row is not None else None


async def list_users(tenant_id: Any | None = None) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    if tenant_id is None:
        rows = await pool.fetch("SELECT * FROM users WHERE deleted_at IS NULL ORDER BY email")
    else:
        rows = await pool.fetch(
            "SELECT * FROM users WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY email",
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
) -> dict[str, Any]:
    """Insert + audit on a caller-supplied connection, already inside a
    transaction — so bootstrap_first_superadmin() can share its lock-holding
    transaction, which create_user()'s own connection could not."""
    row = await conn.fetchrow(
        "INSERT INTO users (email, password_hash, role, tenant_id) "
        "VALUES ($1, $2, $3, $4) RETURNING *",
        email, auth.hash_password(password), role, tenant_id,
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
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
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
    """False == first-time setup, so /auth/bootstrap is open."""
    pool = await db.get_pool()
    return await pool.fetchval(_SUPERADMIN_EXISTS)


async def bootstrap_first_superadmin(*, email: str, password: str) -> dict[str, Any] | None:
    """Creates the very first superadmin. Returns None (no write) if one
    already exists — the caller turns that into a 409.

    Check and insert share one advisory-locked transaction, so two concurrent
    requests can't both see "no superadmin" and both insert. A partial unique
    index would do it too, but would also forbid the legal second superadmin
    created later via POST /users."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _BOOTSTRAP_LOCK_KEY)
            if await conn.fetchval(_SUPERADMIN_EXISTS):
                return None
            # creator_user_id NULL: no authenticated actor created this one.
            return await _insert_user(
                conn,
                email=email,
                password=password,
                role="superadmin",
                tenant_id=None,
                creator_user_id=None,
                creator_user_email=email,
            )


async def authenticate(email: str, password: str) -> dict[str, Any] | None:
    """Returns the user row on success, None on any failure (unknown email,
    wrong password) — deliberately the same shape for both, so a login
    endpoint can't be used to enumerate which emails have accounts."""
    user = await get_user_by_email(email)
    if user is None:
        return None
    if not auth.verify_password(password, user["password_hash"]):
        return None
    return user


async def update_user(
    user_id: Any,
    *,
    actor_user_id: Any | None = None,
    actor_user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    if not fields:
        raise ValueError("update_user() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_user() got non-updatable field(s): {unknown}")

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT * FROM users WHERE id = $1 FOR UPDATE", user_id,
            )
            if old_row is None:
                raise LookupError(f"user {user_id} not found")
            old = dict(old_row)

            columns = list(fields.keys())
            set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(columns))
            new_row = await conn.fetchrow(
                f"UPDATE users SET {set_clause}, updated_at = now() WHERE id = $1 RETURNING *",
                user_id, *(fields[col] for col in columns),
            )
            new = dict(new_row)

            await audit.write_audit(
                conn,
                entity_type="user",
                entity_id=user_id,
                action="updated",
                user_id=actor_user_id,
                user_email=actor_user_email,
                old_value=old,
                new_value=new,
            )
    return new


async def change_password(user_id: Any, *, current_password: str, new_password: str) -> bool:
    """Returns False (no write happens) if current_password doesn't match —
    the router turns that into a 400, distinct from update_user()'s
    role/tenant_id path since this always needs the caller to prove they
    still know the old password, not just be authenticated as someone with
    permission to edit the row."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1 FOR UPDATE", user_id)
            if row is None:
                raise LookupError(f"user {user_id} not found")
            user = dict(row)
            if not auth.verify_password(current_password, user["password_hash"]):
                return False

            new_hash = auth.hash_password(new_password)
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


async def soft_delete_user(
    user_id: Any, *, actor_user_id: Any | None = None, actor_user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT * FROM users WHERE id = $1 FOR UPDATE", user_id,
            )
            if old_row is None:
                raise LookupError(f"user {user_id} not found")
            old = dict(old_row)

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
