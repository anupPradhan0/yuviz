"""
ToolPolicyResolver — DB-aware agent tool enablement, deliberately separate
from ToolRegistry (design review point 7): the registry knows what tools
exist in code; this class knows which of them a given agent may actually
use, resolved from agent_tool_policies/tool_provider_configs.

v1 simplification, flagged explicitly rather than silently done: this
queries Postgres directly with a simple in-process TTL cache, mirroring
TranscriptBuilder's own direct-asyncpg precedent in this same service —
NOT the full Config SDK cache-aside (HTTP + Redis) pattern RuntimeConfig
uses. Promoting this to that pattern (so tool-policy changes propagate the
same way agent config changes do) is the natural v2 hardening step, not
done here to avoid an invasive change to shared libs/config_sdk for a
single small table.

KNOWN CONFLICT, found while building this (2026-07-22), not resolved here:
libs/config_sdk/models.py already has RuntimeConfig.tools: list[ToolSpec],
fed by IConfigProvider.get_tools() — an earlier, pre-existing stub
explicitly labeled "Phase 6b concept, not built yet," always returning []
in both CacheAsideConfigProvider and MockProvider, with no real backing
implementation anywhere. This class is the actual, working implementation
of that same "which tools can an agent use" concept, but it does NOT go
through that seam — agent_tool_policies/tool_provider_configs are an
entirely separate, parallel mechanism. Whoever does the v2 hardening above
should also resolve this: either retire ToolSpec/get_tools() in favor of
this resolver's shape, or re-architect this resolver to flow through that
existing Config SDK seam instead of a direct Postgres query. Left as two
concepts today rather than guessing which one the rest of the codebase
will actually standardize on.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import asyncpg

from .registry import ToolRegistry
from .types import ToolDefinition

log = logging.getLogger(__name__)

_DEFAULT_CACHE_TTL_S = 30.0

# cancel_appointment/reschedule_appointment are deliberately NOT
# independently configurable (2026-07-23 design decision): they're the
# natural counterparts of book_appointment, not separate features a tenant
# would want without it — there's no real scenario where a business offers
# booking but not cancellation/rescheduling. So neither is ever something an
# admin adds/configures via its own tool_provider_config/agent_tool_policies
# row; both are automatically derived here, reusing whichever
# tool_provider_config book_appointment already uses (same Cal.com
# account/event type — CalComCalendarProvider already implements
# check_availability/book_appointment, find_upcoming_bookings/
# cancel_appointment, AND reschedule_appointment on the same instance).
# Disabling book_appointment (agent_tool_policies.enabled=false, or
# removing it entirely) automatically disables both derived tools too, with
# no separate action needed — they simply never appear in `rows` to derive
# from.
_AUTO_DERIVED_COMPANIONS = {"book_appointment": ["cancel_appointment", "reschedule_appointment"]}


@dataclass(frozen=True)
class ResolvedToolPolicy:
    definition:              ToolDefinition
    tool_provider_config_id: str
    engine:                  str
    api_key_ref:             str | None
    extra:                   dict[str, Any]
    timeout_ms:              int | None
    max_calls_per_turn:      int | None


class ToolPolicyResolver:
    def __init__(
        self, pool: asyncpg.Pool | None, registry: ToolRegistry, cache_ttl_s: float = _DEFAULT_CACHE_TTL_S,
    ) -> None:
        self._pool = pool
        self._registry = registry
        self._cache_ttl_s = cache_ttl_s
        self._cache: dict[str, tuple[float, list[ResolvedToolPolicy]]] = {}

    @classmethod
    async def connect(cls, database_url: str | None, registry: ToolRegistry) -> "ToolPolicyResolver":
        if not database_url:
            return cls(None, registry)
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
        return cls(pool, registry)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def enabled_tools(
        self, agent_id: str, only: list[str] | None = None,
    ) -> list[ResolvedToolPolicy]:
        """No agent_id, no pool, or no matching rows all mean the same
        thing: this agent has no tools enabled — a normal, cheap default,
        not an error (same posture as agent_retrieval_policies' own 'no
        row = use the default' contract).

        `only` narrows the result to the named tools — a workflow node's
        own tool list (see docs/workflow.md §5.5). It can only ever remove:
        the DB stays the source of truth for what the agent MAY use, and a
        node narrows that to what this stage may use. A node cannot grant a
        tool the agent doesn't have — that would be privilege escalation
        through the graph editor. An empty list means "no tools this
        stage", the restrictive reading, since withholding capability until
        it's earned is the entire point of stages."""
        if not agent_id or self._pool is None:
            return []

        cached = self._cache.get(agent_id)
        if cached is not None and time.monotonic() - cached[0] < self._cache_ttl_s:
            return _narrow(cached[1], only)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT atp.tool_name, atp.timeout_ms, atp.max_calls_per_turn,
                       tpc.id AS tool_provider_config_id, tpc.engine, tpc.api_key_ref, tpc.extra
                FROM agent_tool_policies atp
                JOIN tool_provider_configs tpc ON tpc.id = atp.tool_provider_config_id
                WHERE atp.agent_id = $1 AND atp.enabled = true AND tpc.deleted_at IS NULL
                """,
                agent_id,
            )

        resolved: list[ResolvedToolPolicy] = []
        for row in rows:
            defn = self._registry.resolve(row["tool_name"])
            if defn is None:
                log.warning(
                    "ToolPolicyResolver: agent_tool_policies references unknown tool_name=%r agent_id=%s — skipping",
                    row["tool_name"], agent_id,
                )
                continue
            raw_extra = row["extra"]
            # asyncpg returns JSONB as a raw string unless a codec is
            # registered on the pool (none is, here or elsewhere in this
            # codebase's Postgres access) — parse defensively rather than
            # assume either shape.
            extra = json.loads(raw_extra) if isinstance(raw_extra, str) else (raw_extra or {})
            resolved.append(ResolvedToolPolicy(
                definition=defn,
                tool_provider_config_id=str(row["tool_provider_config_id"]),
                engine=row["engine"],
                api_key_ref=row["api_key_ref"],
                extra=extra,
                timeout_ms=row["timeout_ms"],
                max_calls_per_turn=row["max_calls_per_turn"],
            ))

        self._add_auto_derived_companions(resolved, agent_id)

        # Cached unnarrowed — `only` varies per node within a single call,
        # and caching per (agent, node) would multiply entries for what is
        # a list comprehension over at most a handful of tools.
        self._cache[agent_id] = (time.monotonic(), resolved)
        return _narrow(resolved, only)

    def _add_auto_derived_companions(self, resolved: list[ResolvedToolPolicy], agent_id: str) -> None:
        """See _AUTO_DERIVED_COMPANIONS. An explicit row for a derived tool
        (if one somehow exists) always wins — this only fills a gap, never
        overrides an admin's own configuration."""
        present = {p.definition.name for p in resolved}
        for source_name, derived_names in _AUTO_DERIVED_COMPANIONS.items():
            if source_name not in present:
                continue
            source = next(p for p in resolved if p.definition.name == source_name)
            for derived_name in derived_names:
                if derived_name in present:
                    continue
                derived_defn = self._registry.resolve(derived_name)
                if derived_defn is None:
                    log.warning(
                        "ToolPolicyResolver: _AUTO_DERIVED_COMPANIONS references unknown tool_name=%r agent_id=%s",
                        derived_name, agent_id,
                    )
                    continue
                resolved.append(ResolvedToolPolicy(
                    definition=derived_defn,
                    tool_provider_config_id=source.tool_provider_config_id,
                    engine=source.engine,
                    api_key_ref=source.api_key_ref,
                    extra=source.extra,
                    timeout_ms=source.timeout_ms,
                    max_calls_per_turn=source.max_calls_per_turn,
                ))
                present.add(derived_name)


def _narrow(
    resolved: list[ResolvedToolPolicy], only: list[str] | None,
) -> list[ResolvedToolPolicy]:
    """See enabled_tools()'s `only`. A companion tool rides along with its
    source (see _AUTO_DERIVED_COMPANIONS): a node that offers
    book_appointment offers cancel/reschedule too, exactly as the agent-wide
    resolution does — they were never independently selectable, so a node
    listing them separately isn't something an operator can even express."""
    if only is None:
        return resolved
    allowed = set(only)
    for source_name, derived_names in _AUTO_DERIVED_COMPANIONS.items():
        if source_name in allowed:
            allowed.update(derived_names)
    return [p for p in resolved if p.definition.name in allowed]
