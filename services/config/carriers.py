"""
Carriers (BYOC) CRUD — cold-path admin config only, same no-cache posture
as tool_provider_configs.py: nothing on the call path reads carriers (the
Gateway/Conversation Service never touch this table; only the DID Service
reads it, to authenticate its own carrier API calls — a cold, low-frequency
path, not worth a Redis cache-aside layer for).

Returning auth_token_ref to a caller is fine: it's a reference path (e.g.
'env:PLIVO_AUTH_TOKEN'), never a resolved secret — same convention as
provider_configs.api_key_ref.
"""

from __future__ import annotations

from typing import Any

from . import audit, db

_UPDATABLE_FIELDS = {"name", "auth_id", "auth_token_ref", "carrier_account_ref"}


async def get_carrier_by_id(carrier_id: Any) -> dict[str, Any] | None:
    """Not cached — same reasoning as tenants.get_tenant_by_id(): a cold,
    low-frequency existence check before an insert that FK-references
    carriers, not a hot path."""
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM carriers WHERE id = $1 AND deleted_at IS NULL", carrier_id,
    )
    return dict(row) if row is not None else None


async def list_carriers(tenant_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT * FROM carriers WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY name",
        tenant_id,
    )
    return [dict(row) for row in rows]


async def create_carrier(
    *,
    tenant_id: Any,
    name: str,
    provider: str,
    auth_id: str | None = None,
    auth_token_ref: str | None = None,
    carrier_account_ref: str | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO carriers (tenant_id, name, provider, auth_id, auth_token_ref, carrier_account_ref) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
                tenant_id, name, provider, auth_id, auth_token_ref, carrier_account_ref,
            )
            result = dict(row)
            await audit.write_audit(
                conn,
                entity_type="carrier",
                entity_id=result["id"],
                action="created",
                user_id=user_id,
                user_email=user_email,
                new_value=result,
            )
    return result


async def update_carrier(
    carrier_id: Any,
    *,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    if not fields:
        raise ValueError("update_carrier() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_carrier() got non-updatable field(s): {unknown}")

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT * FROM carriers WHERE id = $1 FOR UPDATE", carrier_id,
            )
            if old_row is None:
                raise LookupError(f"carrier {carrier_id} not found")
            old = dict(old_row)

            columns = list(fields.keys())
            set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(columns))
            new_row = await conn.fetchrow(
                f"UPDATE carriers SET {set_clause}, updated_at = now() WHERE id = $1 RETURNING *",
                carrier_id, *(fields[col] for col in columns),
            )
            new = dict(new_row)

            # Scoped to the written columns, not the full row — otherwise
            # auth_token_ref (redacted either way) rides along on every
            # update and the UI can't tell "redacted, unchanged" from
            # "redacted, changed."
            await audit.write_audit(
                conn,
                entity_type="carrier",
                entity_id=carrier_id,
                action="updated",
                user_id=user_id,
                user_email=user_email,
                old_value={col: old[col] for col in columns},
                new_value={col: new[col] for col in columns},
            )
    return new


async def soft_delete_carrier(
    carrier_id: Any, *, user_id: Any | None = None, user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT * FROM carriers WHERE id = $1 FOR UPDATE", carrier_id,
            )
            if old_row is None:
                raise LookupError(f"carrier {carrier_id} not found")
            old = dict(old_row)

            await conn.execute(
                "UPDATE carriers SET deleted_at = now() WHERE id = $1", carrier_id,
            )
            await audit.write_audit(
                conn,
                entity_type="carrier",
                entity_id=carrier_id,
                action="deleted",
                user_id=user_id,
                user_email=user_email,
                old_value=old,
            )
