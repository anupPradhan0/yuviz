"""
Agent CRUD — same cache-aside + audited-mutation pattern as tenants.py.

get_agent() is keyed by (tenant_slug, agent_slug) rather than a bare id,
because that's what the hot path actually has: the WebSocket path is
`/<agent>/<uuid>`, and the tenant is resolved from the same connection
context — nobody holds a UUID before the call starts. get_agent_by_id()
exists for the Admin UI's edit-by-id flow, where the id is already known.
"""

from __future__ import annotations

from typing import Any

from . import audit, cache, db

_UPDATABLE_FIELDS = {
    "name", "greeting", "system_prompt", "goodbye_grace_ms", "language",
    "stt_config_id", "llm_config_id", "tts_config_id",
    "transfer_type", "transfer_destination", "queue_id", "escalation_threshold",
    "caller_id_policy", "platform_did", "custom_caller_id",
    "transfer_waiting_experience",
    "end_call_prompt", "transfer_prompt",
    "farewell_message", "transfer_announcement",
    "status",
}


def _cache_key(tenant_slug: str, agent_slug: str) -> str:
    return f"agent:{tenant_slug}:{agent_slug}"


async def get_agent(tenant_slug: str, agent_slug: str) -> dict[str, Any] | None:
    cached = await cache.get_json(_cache_key(tenant_slug, agent_slug))
    if cached is not None:
        return cached

    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT a.* FROM agents a JOIN tenants t ON t.id = a.tenant_id "
        "WHERE t.slug = $1 AND a.slug = $2 AND a.deleted_at IS NULL AND t.deleted_at IS NULL",
        tenant_slug, agent_slug,
    )
    if row is None:
        return None

    result = dict(row)
    await cache.set_json(_cache_key(tenant_slug, agent_slug), result)
    return result


async def get_agent_by_id(agent_id: Any) -> dict[str, Any] | None:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM agents WHERE id = $1 AND deleted_at IS NULL", agent_id,
    )
    return dict(row) if row is not None else None


async def list_agents(tenant_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT * FROM agents WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY name",
        tenant_id,
    )
    return [dict(row) for row in rows]


async def create_agent(
    *,
    tenant_id: Any,
    slug: str,
    name: str,
    greeting: str = "",
    system_prompt: str = "",
    tenant_slug: str | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO agents (tenant_id, slug, name, greeting, system_prompt) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING *",
                tenant_id, slug, name, greeting, system_prompt,
            )
            result = dict(row)
            await audit.write_audit(
                conn,
                entity_type="agent",
                entity_id=result["id"],
                action="created",
                user_id=user_id,
                user_email=user_email,
                new_value=result,
            )
    if tenant_slug is not None:
        # Warm the cache immediately rather than leaving it for the agent's
        # first real call to populate lazily — same reasoning, and the same
        # real live-call failure this exact gap already caused, as
        # phone_numbers.create_phone_number()'s identical fix (see project
        # memory 2026-07-13). Optional (not required) because most existing
        # callers only have tenant_id on hand; the REST router (the actual
        # live-usage path) does have tenant_slug and passes it.
        await get_agent(tenant_slug, slug)
    return result


async def update_agent(
    agent_id: Any,
    *,
    tenant_slug: str,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """tenant_slug is required so the correct cache key can be invalidated —
    it's not derivable from agent_id alone without an extra query, and the
    caller (Admin UI / API layer) already has it from the request context."""
    if not fields:
        raise ValueError("update_agent() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_agent() got non-updatable field(s): {unknown}")

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # FOR UPDATE OF a is two fixes in one: it locks the agent row for
            # the rest of this transaction (so a concurrent update can't read
            # a stale "old" value for the audit log — see project memory's
            # audit-race note), and the join against tenants scopes the
            # lookup by tenant_slug — an agent_id that exists but belongs to
            # a *different* tenant is indistinguishable from "doesn't exist"
            # to this caller. Previously this was scoped by agent_id alone,
            # which let any tenant's URL path update or delete any other
            # tenant's agent by id (cross-tenant hijack).
            old_row = await conn.fetchrow(
                "SELECT a.* FROM agents a JOIN tenants t ON t.id = a.tenant_id "
                "WHERE a.id = $1 AND t.slug = $2 FOR UPDATE OF a",
                agent_id, tenant_slug,
            )
            if old_row is None:
                raise LookupError(f"agent {agent_id} not found under tenant {tenant_slug!r}")
            old = dict(old_row)

            columns = list(fields.keys())
            set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(columns))
            new_row = await conn.fetchrow(
                f"UPDATE agents SET {set_clause} WHERE id = $1 RETURNING *",
                agent_id, *(fields[col] for col in columns),
            )
            new = dict(new_row)

            await audit.write_audit(
                conn,
                entity_type="agent",
                entity_id=agent_id,
                action="updated",
                user_id=user_id,
                user_email=user_email,
                old_value=old,
                new_value=new,
            )

    await cache.invalidate(_cache_key(tenant_slug, old["slug"]))
    return new


async def soft_delete_agent(
    agent_id: Any,
    *,
    tenant_slug: str,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # See update_agent()'s comment — same tenant-scoping + row-lock fix.
            old_row = await conn.fetchrow(
                "SELECT a.* FROM agents a JOIN tenants t ON t.id = a.tenant_id "
                "WHERE a.id = $1 AND t.slug = $2 FOR UPDATE OF a",
                agent_id, tenant_slug,
            )
            if old_row is None:
                raise LookupError(f"agent {agent_id} not found under tenant {tenant_slug!r}")
            old = dict(old_row)

            await conn.execute("UPDATE agents SET deleted_at = now() WHERE id = $1", agent_id)
            await audit.write_audit(
                conn,
                entity_type="agent",
                entity_id=agent_id,
                action="deleted",
                user_id=user_id,
                user_email=user_email,
                old_value=old,
            )

    await cache.invalidate(_cache_key(tenant_slug, old["slug"]))
