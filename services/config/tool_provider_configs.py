"""
tool_provider_configs CRUD — cold-path admin config only. Unlike
provider_configs.py, this deliberately has NO Redis cache: nothing on the
call path reads through Config Service for this table. The Conversation
Service's ToolPolicyResolver (services/conversation/tools/policy_resolver.py)
queries Postgres directly with its own short-lived in-process TTL cache, a
documented v1 simplification — see that file's docstring for the known
conflict with libs/config_sdk's unrelated ToolSpec/get_tools() stub.

Same audited-mutation pattern as provider_configs.py otherwise. Returning
api_key_ref to a caller is fine: it's a reference path (e.g.
'env:CAL_API_KEY'), never a resolved secret.
"""

from __future__ import annotations

from typing import Any

from . import audit, db

_UPDATABLE_FIELDS = {"name", "engine", "api_key_ref", "extra"}


async def get_tool_provider_config(tool_provider_config_id: Any) -> dict[str, Any] | None:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM tool_provider_configs WHERE id = $1 AND deleted_at IS NULL",
        tool_provider_config_id,
    )
    return dict(row) if row is not None else None


async def list_tool_provider_configs(tenant_id: Any, *, tool_name: str | None = None) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    conditions = ["tenant_id = $1", "deleted_at IS NULL"]
    params: list[Any] = [tenant_id]
    if tool_name is not None:
        params.append(tool_name)
        conditions.append(f"tool_name = ${len(params)}")

    rows = await pool.fetch(
        f"SELECT * FROM tool_provider_configs WHERE {' AND '.join(conditions)} ORDER BY name",
        *params,
    )
    return [dict(row) for row in rows]


async def create_tool_provider_config(
    *,
    tenant_id: Any,
    name: str,
    tool_name: str,
    engine: str,
    api_key_ref: str | None = None,
    extra: dict[str, Any] | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    import json as _json

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO tool_provider_configs (tenant_id, name, tool_name, engine, api_key_ref, extra) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb) RETURNING *",
                tenant_id, name, tool_name, engine, api_key_ref,
                _json.dumps(extra) if extra is not None else None,
            )
            result = dict(row)
            await audit.write_audit(
                conn,
                entity_type="tool_provider_config",
                entity_id=result["id"],
                action="created",
                user_id=user_id,
                user_email=user_email,
                new_value=result,
            )
    return result


async def update_tool_provider_config(
    tool_provider_config_id: Any,
    *,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    if not fields:
        raise ValueError("update_tool_provider_config() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_tool_provider_config() got non-updatable field(s): {unknown}")

    import json as _json
    if "extra" in fields and fields["extra"] is not None:
        fields = {**fields, "extra": _json.dumps(fields["extra"])}

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT * FROM tool_provider_configs WHERE id = $1 FOR UPDATE", tool_provider_config_id,
            )
            if old_row is None:
                raise LookupError(f"tool_provider_config {tool_provider_config_id} not found")
            old = dict(old_row)

            columns = list(fields.keys())
            set_parts = []
            for i, col in enumerate(columns):
                cast = "::jsonb" if col == "extra" else ""
                set_parts.append(f"{col} = ${i + 2}{cast}")
            new_row = await conn.fetchrow(
                f"UPDATE tool_provider_configs SET {', '.join(set_parts)}, updated_at = now() "
                f"WHERE id = $1 RETURNING *",
                tool_provider_config_id, *(fields[col] for col in columns),
            )
            new = dict(new_row)

            await audit.write_audit(
                conn,
                entity_type="tool_provider_config",
                entity_id=tool_provider_config_id,
                action="updated",
                user_id=user_id,
                user_email=user_email,
                old_value=old,
                new_value=new,
            )
    return new


async def soft_delete_tool_provider_config(
    tool_provider_config_id: Any, *, user_id: Any | None = None, user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT * FROM tool_provider_configs WHERE id = $1 FOR UPDATE", tool_provider_config_id,
            )
            if old_row is None:
                raise LookupError(f"tool_provider_config {tool_provider_config_id} not found")
            old = dict(old_row)

            await conn.execute(
                "UPDATE tool_provider_configs SET deleted_at = now() WHERE id = $1", tool_provider_config_id,
            )
            await audit.write_audit(
                conn,
                entity_type="tool_provider_config",
                entity_id=tool_provider_config_id,
                action="deleted",
                user_id=user_id,
                user_email=user_email,
                old_value=old,
            )
