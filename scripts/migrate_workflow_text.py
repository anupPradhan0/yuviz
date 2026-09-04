#!/usr/bin/env python3
"""
Moves an agent's conversation text into its graph, then lets schema.sql drop
the columns it came from (docs/workflow.md §9.1).

Six columns held text that duplicated or overrode the graph — greeting,
system_prompt, end_call_prompt, farewell_message, transfer_prompt,
transfer_announcement. The first two move: the greeting onto the start node,
the system prompt onto a new global node. The other four are deleted outright
— they configured the wording of the [[END_CALL]]/[[TRANSFER]] safety nets,
which are now fixed, and the words an agent actually speaks when ending or
transferring are its end and transfer steps' own prompts.

Runs BEFORE schema.sql (see deployment/sh/init.sh), because schema.sql is
where the DROP COLUMN lives and the data has to be out first. Idempotent
twice over: it exits cleanly when the columns are already gone (every boot
after the first), and it skips any agent whose graph already has a global
node, so a half-finished run resumes rather than double-applying.

Only `workflow` and `workflow_draft` are rewritten. agent_workflow_versions
is left alone on purpose — it is an append-only record of what was actually
published at the time, and rewriting history to look like it always had a
global node would make "what was live at 3pm yesterday" a lie.
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

# The destination columns, created here rather than waited for. This script
# runs BEFORE schema.sql (see the module docstring), so on an upgrade — where
# `agents` already exists but predates this branch — they are not there yet
# and the SELECT below would raise UndefinedColumnError, which under
# init.sh's `set -euo pipefail` takes down the whole init: no schema, no
# service account, no seed. Idempotent, and schema.sql re-issues the same
# two statements harmlessly afterwards.
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
    """The graph, with the greeting on its start node and the system prompt
    on a global node. A None graph becomes a whole starter graph: an agent
    that was single-prompt has to come out of this with a flow, or Phase 4's
    fallback would silently run one for it on every call, invisible in the
    editor and impossible to edit.

    Returns None when there is nothing to do, so the caller can skip the
    write entirely rather than bump config_version for a no-op.
    """
    if not graph or not isinstance(graph.get("nodes"), list):
        return starter_graph(greeting, system_prompt, tools)

    nodes = graph["nodes"]
    if any(n.get("type") == "global" for n in nodes):
        return None                      # already migrated

    out = {**graph, "nodes": [dict(n) for n in nodes]}
    if system_prompt.strip():
        # Parked to the right of the flow rather than in it — it is not a
        # step, and autoLayout leaves unconnected nodes where they are.
        out["nodes"].append({
            "id": "global", "type": "global", "position": {"x": 420, "y": 0},
            "data": {"name": "always applies", "prompt": system_prompt},
        })
    # The start node's own greeting already won over the column at runtime,
    # so only fill it in where the node left it blank — otherwise this would
    # overwrite the graph with the value it was already overriding.
    #
    # Its tools are seeded the same way, and for a sharper reason: Node.tools
    # is default-deny (see WorkflowRunner.allowed_tool_names), so a step with
    # no `tools` key offers nothing. An agent that had book_appointment
    # enabled and then gained a graph would silently stop being able to book
    # — a behaviour change no operator asked for and nothing would report.
    # Only where the key is absent: an operator who has already narrowed this
    # step in the editor meant it.
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

        # Same reason the destination columns are created above: this runs
        # before schema.sql, so on a database predating the tool framework
        # agent_tool_policies does not exist yet. No table, no tools to
        # preserve — which is correct, not a failure.
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
            # COALESCE so a column already migrated on an earlier, partial
            # run is left exactly as it is rather than recomputed.
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
    return json.loads(value) if isinstance(value, str) else value


if __name__ == "__main__":
    asyncio.run(main())
