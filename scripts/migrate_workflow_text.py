#!/usr/bin/env python3
"""Move agent conversation text into the graph before schema.sql DROP COLUMN.

greeting → start node; system_prompt → global node. end_call/farewell/transfer
prompt columns are dropped (wording is fixed / lives on end+transfer steps).

Runs before schema.sql (init.sh). Idempotent: no-op when columns gone or graph
already has a global node. Does not rewrite agent_workflow_versions history.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.config_sdk.workflow import starter_graph  # noqa: E402

_MOVED_COLUMNS = ("greeting", "system_prompt")

_HAS_COLUMNS = """
SELECT COUNT(*) FROM information_schema.columns
 WHERE table_name = 'agents' AND column_name = ANY($1::text[])
"""

# Create destination cols here: this runs before schema.sql; missing cols abort init.
_ADD_DESTINATION_COLUMNS = (
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS workflow       JSONB",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS workflow_draft JSONB",
)


_ENABLED_TOOLS = """
SELECT tool_name FROM agent_tool_policies WHERE agent_id = $1 AND enabled ORDER BY tool_name
"""


def _with_text(
    graph: dict | None, greeting: str, system_prompt: str, tools: list[str],
) -> dict | None:
    """Put greeting on start and system_prompt on a global node; starter if no graph.
    Returns None when already migrated (skip write / config_version bump)."""
    if not graph or not isinstance(graph.get("nodes"), list):
        return starter_graph(greeting, system_prompt, tools)

    nodes = graph["nodes"]
    if any(n.get("type") == "global" for n in nodes):
        return None  # already migrated

    out = {**graph, "nodes": [dict(n) for n in nodes]}
    if system_prompt.strip():
        # Unconnected; autoLayout leaves it where parked.
        out["nodes"].append({
            "id": "global", "type": "global", "position": {"x": 420, "y": 0},
            "data": {"name": "always applies", "prompt": system_prompt},
        })
    # Fill start greeting/tools only when blank/absent — Node.tools is default-deny.
    for node in out["nodes"]:
        if node.get("type") != "start":
            continue
        data = dict(node.get("data") or {})
        changed = False
        if greeting.strip() and not (data.get("greeting") or "").strip():
            data["greeting"] = greeting
            changed = True
        if tools and not isinstance(data.get("tools"), list):
            data["tools"] = list(tools)
            changed = True
        if changed:
            node["data"] = data
        break
    return out


async def main() -> None:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("  ✗ POSTGRES_DSN not set", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(dsn)
    try:
        table_exists = await conn.fetchval(
            "SELECT to_regclass('public.agents') IS NOT NULL"
        )
        if not table_exists:
            print("  ✓ no agents table yet — nothing to migrate")
            return
        if await conn.fetchval(_HAS_COLUMNS, list(_MOVED_COLUMNS)) == 0:
            print("  ✓ already migrated")
            return

        for statement in _ADD_DESTINATION_COLUMNS:
            await conn.execute(statement)

        # Pre-schema DBs may lack agent_tool_policies — no table means no tools to keep.
        has_policies = await conn.fetchval(
            "SELECT to_regclass('public.agent_tool_policies') IS NOT NULL"
        )

        rows = await conn.fetch(
            "SELECT id, slug, greeting, system_prompt, workflow, workflow_draft FROM agents"
        )
        migrated = 0
        for row in rows:
            greeting = row["greeting"] or ""
            system_prompt = row["system_prompt"] or ""
            tools = (
                [r["tool_name"] for r in await conn.fetch(_ENABLED_TOOLS, row["id"])]
                if has_policies else []
            )
            live = _with_text(_loads(row["workflow"]), greeting, system_prompt, tools)
            draft = _with_text(_loads(row["workflow_draft"]), greeting, system_prompt, tools)
            if live is None and draft is None:
                continue
            # COALESCE: partial prior run must not recompute an already-migrated column.
            await conn.execute(
                "UPDATE agents SET workflow = COALESCE($2::jsonb, workflow), "
                "workflow_draft = COALESCE($3::jsonb, workflow_draft) WHERE id = $1",
                row["id"],
                json.dumps(live) if live is not None else None,
                json.dumps(draft) if draft is not None else None,
            )
            migrated += 1
            print(f"  ✓ {row['slug']}")
        print(f"  ✓ moved conversation text into {migrated} graph(s)")
    finally:
        await conn.close()


def _loads(value) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return json.loads(value)


if __name__ == "__main__":
    asyncio.run(main())
