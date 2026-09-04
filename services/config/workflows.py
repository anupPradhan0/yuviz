"""
Workflow draft/publish/versions — the write side of agents.workflow (see
docs/workflow.md §4.2).

Three states, and the split is the whole point:

  workflow_draft            the editor autosaves here. May be invalid, may
                            be half-drawn. Never read by a call.
  workflow                  what live calls execute. Only ever written by
                            publish() (and create_agent, which publishes the
                            starter graph), and publish validates first — a
                            broken graph cannot reach a phone call. Never
                            NULL: an agent IS its workflow, so there is no
                            un-publish that would leave it with nothing to
                            run.
  agent_workflow_versions   every publish appends. Rollback republishes an
                            old version as a new one; the log is never
                            rewritten, so "what was live at 3pm yesterday"
                            stays answerable.

Deliberately not routed through agents.update_agent(): `workflow` is not an
operator-settable field, it is the output of a validated publish, and
keeping it out of _UPDATABLE_FIELDS is what guarantees no PATCH can slip an
unvalidated graph onto a live agent. The cache-invalidation and audit
discipline is the same as that module's, though — same key, same
in-transaction audit write.
"""

from __future__ import annotations

import json
from typing import Any

from libs.config_sdk.workflow import (
    WorkflowError,
    WorkflowInvalid,
    graph_warnings,
    parse_graph,
)

from . import agents as agents_service
from . import audit, cache, db


class WorkflowValidationError(Exception):
    """Carries the structured per-node/per-edge errors the editor paints
    the canvas with — never a single flattened string (see
    docs/workflow.md §5.1)."""

    def __init__(self, errors: list[WorkflowError]) -> None:
        self.errors = errors
        super().__init__("workflow is not valid")


def validate(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Errors block a publish; warnings never do. Returns the warnings and
    raises WorkflowValidationError on any error."""
    try:
        parsed = parse_graph(graph)
    except WorkflowInvalid as exc:
        raise WorkflowValidationError(exc.errors) from None
    return [w.to_dict() for w in graph_warnings(parsed)]


async def _locked_agent(conn: Any, agent_id: Any, tenant_slug: str) -> dict[str, Any]:
    """Same tenant-scoped SELECT ... FOR UPDATE as agents.update_agent() —
    an agent_id belonging to another tenant is indistinguishable from
    "doesn't exist", and the lock keeps a concurrent publish from reading a
    stale version number."""
    row = await conn.fetchrow(
        "SELECT a.* FROM agents a JOIN tenants t ON t.id = a.tenant_id "
        "WHERE a.id = $1 AND t.slug = $2 AND a.deleted_at IS NULL FOR UPDATE OF a",
        agent_id, tenant_slug,
    )
    if row is None:
        raise LookupError(f"agent {agent_id} not found under tenant {tenant_slug!r}")
    return dict(row)


def _as_graph(value: Any) -> dict[str, Any] | None:
    # Shared with agents.py and calls.py, which have the same JSONB-comes-
    # back-as-a-string problem — see db.json_col for why no pool-wide codec.
    return db.json_col(value)


async def get_workflow(agent_id: Any, tenant_slug: str) -> dict[str, Any]:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT a.workflow, a.workflow_draft FROM agents a JOIN tenants t ON t.id = a.tenant_id "
        "WHERE a.id = $1 AND t.slug = $2 AND a.deleted_at IS NULL",
        agent_id, tenant_slug,
    )
    if row is None:
        raise LookupError(f"agent {agent_id} not found under tenant {tenant_slug!r}")
    published = _as_graph(row["workflow"])
    return {
        "workflow": published,
        "workflow_draft": _as_graph(row["workflow_draft"]),
        "published": published is not None,
    }


async def save_draft(
    agent_id: Any, *, tenant_slug: str, graph: dict[str, Any],
) -> dict[str, Any]:
    """Autosave target. Never validated, never audited: a draft is
    keystrokes, not a config change — auditing every debounced save would
    bury the publish that actually matters. No cache invalidation either,
    since nothing caches or reads a draft."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await _locked_agent(conn, agent_id, tenant_slug)
        await conn.execute(
            "UPDATE agents SET workflow_draft = $2::jsonb WHERE id = $1",
            agent_id, json.dumps(graph),
        )
    return {"saved": True}


async def publish(
    agent_id: Any,
    *,
    tenant_slug: str,
    graph: dict[str, Any] | None = None,
    note: str | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    """Validates, then writes the live graph, appends a version, and bumps
    config_version through the existing agents_version trigger — which is
    exactly what makes the graph propagate over the Redis invalidation path
    every other agent field already uses.

    graph=None publishes whatever is in workflow_draft, which is what the
    editor's Publish button does; passing one explicitly is for rollback
    (see rollback()) and API callers with no draft.
    """
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await _locked_agent(conn, agent_id, tenant_slug)
            candidate = graph if graph is not None else _as_graph(old["workflow_draft"])
            if candidate is None:
                raise ValueError("nothing to publish — this agent has no workflow draft")
            warnings = validate(candidate)

            version = await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM agent_workflow_versions WHERE agent_id = $1",
                agent_id,
            )
            payload = json.dumps(candidate)
            new_row = await conn.fetchrow(
                "UPDATE agents SET workflow = $2::jsonb, workflow_draft = $2::jsonb "
                "WHERE id = $1 RETURNING *",
                agent_id, payload,
            )
            await conn.execute(
                "INSERT INTO agent_workflow_versions (agent_id, version, graph, published_by, note) "
                "VALUES ($1, $2, $3::jsonb, $4, $5)",
                agent_id, version, payload, user_id, note,
            )
            await audit.write_audit(
                conn,
                entity_type="agent_workflow",
                entity_id=agent_id,
                action="updated",
                user_id=user_id,
                user_email=user_email,
                old_value={"workflow": _as_graph(old["workflow"])},
                new_value={"workflow": candidate, "version": version},
            )
            new = dict(new_row)

    await cache.invalidate(agents_service._cache_key(tenant_slug, new["slug"]))
    return {
        "version": version,
        "config_version": new["config_version"],
        "warnings": warnings,
    }




async def list_versions(agent_id: Any, tenant_slug: str, limit: int = 50) -> list[dict[str, Any]]:
    """Summaries, not graphs — the version list is a picker, and shipping
    every full graph would make it heavy for no reason. get_version()
    fetches the one an operator actually wants back."""
    pool = await db.get_pool()
    rows = await pool.fetch(
        """
        SELECT v.id, v.version, v.published_at, v.note, u.email AS published_by_email,
               jsonb_array_length(COALESCE(v.graph -> 'nodes', '[]'::jsonb)) AS node_count,
               jsonb_array_length(COALESCE(v.graph -> 'edges', '[]'::jsonb)) AS edge_count
        FROM agent_workflow_versions v
        JOIN agents a ON a.id = v.agent_id
        JOIN tenants t ON t.id = a.tenant_id
        LEFT JOIN users u ON u.id = v.published_by
        WHERE v.agent_id = $1 AND t.slug = $2
        ORDER BY v.version DESC
        LIMIT $3
        """,
        agent_id, tenant_slug, limit,
    )
    return [dict(row) for row in rows]


async def get_version(agent_id: Any, tenant_slug: str, version: int) -> dict[str, Any] | None:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT v.* FROM agent_workflow_versions v "
        "JOIN agents a ON a.id = v.agent_id JOIN tenants t ON t.id = a.tenant_id "
        "WHERE v.agent_id = $1 AND t.slug = $2 AND v.version = $3",
        agent_id, tenant_slug, version,
    )
    if row is None:
        return None
    result = dict(row)
    result["graph"] = _as_graph(result["graph"])
    return result


async def rollback(
    agent_id: Any, *, tenant_slug: str, version: int,
    user_id: Any | None = None, user_email: str | None = None,
) -> dict[str, Any]:
    """Republishes an old version as a NEW one — append-only, never a
    rewrite of history."""
    old_version = await get_version(agent_id, tenant_slug, version)
    if old_version is None:
        raise LookupError(f"workflow version {version} not found for agent {agent_id}")
    return await publish(
        agent_id, tenant_slug=tenant_slug, graph=old_version["graph"],
        note=f"rollback to version {version}", user_id=user_id, user_email=user_email,
    )
