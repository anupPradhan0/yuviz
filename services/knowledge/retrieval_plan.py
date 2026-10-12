"""Per-agent resolved retrieval plan: policy, enabled active KBs grouped by embedding config, the embedding
configs themselves and the prompt-mode documents. Resolving it takes four queries; caching it makes the hot path
database-free until the search itself.

Invalidation. knowledge_bases.config_version is bumped by a trigger, but agent_knowledge_bases and
agent_retrieval_policies carry no version, and reading any version is itself a DB call, so the plan is keyed
by (tenant, agent) and invalidated two ways: (1) explicit invalidate_agent()/invalidate_tenant(), called by the
writers in this process (agent_kb, knowledge_bases, retrieval_policies); (2) a short TTL (KNOWLEDGE_PLAN_TTL_S,
default 30 s) that bounds staleness for writes made by another process, e.g. the ingestion worker finishing a
prompt-mode document. prewarm() resolves a plan ahead of the first turn."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from libs.tenancy import TenantScopeConflict, current_tenant, tenant_conn

from . import cache, db
from .embedding_manager import EmbeddingProviderConfig, EmbeddingProviderManager
from .vector_repository import SEARCHABLE_STATUSES, VectorMatch

log = logging.getLogger(__name__)

SYSTEM_DEFAULT_POLICY: dict[str, Any] = {
    "top_k": 5,
    "max_tokens": 1000,
    "minimum_score": 0.3,
    "rerank": False,
    # Hybrid is ON by default: kb_chunks.tsv is a generated column, so it is populated for every chunk and
    # costs nothing to opt into; dense-only misses exact terms (SKUs, names, numbers) a voice agent is asked
    # about all the time. An agent row with hybrid_search = false still forces dense-only.
    "hybrid_search": True,
    "include_citations": True,
}


def merge_policy(agent_row: Any, overrides: dict[str, Any]) -> dict[str, Any]:
    """call override > agent row > system default."""
    resolved: dict[str, Any] = {}
    for field_name, system_default in SYSTEM_DEFAULT_POLICY.items():
        override_value = overrides.get(field_name)
        if override_value is not None:
            resolved[field_name] = override_value
        elif agent_row is not None and agent_row[field_name] is not None:
            resolved[field_name] = agent_row[field_name]
        else:
            resolved[field_name] = system_default
    return resolved


@dataclass(frozen=True)
class RetrievalPlan:
    tenant_slug: str  # the slugs this plan was resolved for: every cache hit must match them exactly
    agent_slug: str
    tenant_id: str
    agent_id: str
    agent_policy: dict[str, Any]  # system default + agent row; call overrides are applied per request
    kb_ids: list[str]  # every enabled, active KB (lexical search and prompt-mode cover KBs with no embeddings)
    groups: dict[str, list[str]]  # embedding_config_id -> KB ids embedded with it
    embedding_configs: dict[str, EmbeddingProviderConfig]
    prompt_matches: list[VectorMatch]
    kb_versions: dict[str, int] = field(default_factory=dict)
    chunk_count: int = 0  # searchable child chunks across kb_ids; lets lexical search skip DF probing on small KBs
    # Static degradation discovered while resolving (e.g. a KB whose embedding config was deleted).
    degraded: tuple[str, ...] = ()


_PLAN_TTL_S = float(os.environ.get("KNOWLEDGE_PLAN_TTL_S", "30"))
_plans = cache.TTLCache(ttl_s=_PLAN_TTL_S, max_entries=int(os.environ.get("KNOWLEDGE_PLAN_CACHE_MAX", "2048")))
_loaded_at: dict[str, float] = {}  # key -> monotonic time of the load, for stale-while-revalidate
_refreshing: set[str] = set()
_MAX_BACKGROUND_REFRESHES = 8
# Bumped by every invalidation. A load that started before a bump must not be stored: it may carry the data
# the invalidation was meant to evict.
_generation = 0


def invalidate_agent(tenant_slug: str, agent_slug: str) -> None:
    global _generation
    _generation += 1
    _plans.delete(cache.plan_key(tenant_slug, agent_slug))


def invalidate_tenant(tenant_slug: str) -> None:
    global _generation
    _generation += 1
    _plans.delete_prefix(cache.plan_tenant_prefix(tenant_slug))


def clear_cache() -> None:
    global _generation
    _generation += 1
    _plans.clear()
    _loaded_at.clear()


def check_scope(plan: "RetrievalPlan") -> None:
    """The ambient scope (caller's tenant, or the router-set target for platform-scoped actors such as the
    Conversation service account) must be the plan's tenant. Applied on cold fills and hits alike."""
    effective = current_tenant()
    if effective is not None and str(effective) not in (plan.tenant_id, plan.tenant_slug):
        raise TenantScopeConflict(f"plan belongs to tenant {plan.tenant_id!r}, scope is {effective!r}")


def cached_plan(tenant_slug: str, agent_slug: str) -> "RetrievalPlan | None":
    key = cache.plan_key(tenant_slug, agent_slug)
    plan = _plans.get(key)
    if plan is None:
        return None
    if plan.tenant_slug != tenant_slug or plan.agent_slug != agent_slug:
        _plans.delete(key)  # belt and braces behind the injective key: never serve a plan for other slugs
        return None
    check_scope(plan)
    if time.monotonic() - _loaded_at.get(key, 0.0) > _PLAN_TTL_S / 2:
        _refresh_in_background(key, plan)
    return plan


def _refresh_in_background(key: str, plan: "RetrievalPlan") -> None:
    """Stale-while-revalidate: past TTL/2 the cached plan is still served while one bounded background task
    reloads it, so a live agent does not pay a cold plan whenever the hard TTL lapses. Failure keeps the old
    plan until the hard TTL evicts it."""
    if key in _refreshing or len(_refreshing) >= _MAX_BACKGROUND_REFRESHES:
        return
    _refreshing.add(key)
    generation = _generation

    async def run() -> None:
        try:
            pool = await db.get_pool()
            async with tenant_conn(pool, explicit_tenant=plan.tenant_id, reason="retrieval-search") as conn:
                fresh = await load_plan(conn, plan.tenant_slug, plan.agent_slug)
            if fresh is not None and generation == _generation:
                _store(key, fresh)
        except Exception:  # noqa: BLE001
            log.warning("retrieval plan refresh failed agent=%s", plan.agent_id, exc_info=True)
        finally:
            _refreshing.discard(key)

    try:
        # Empty context: the refresh must not run under whichever request happened to trigger it.
        asyncio.get_running_loop().create_task(run(), context=contextvars.Context())
    except RuntimeError:
        _refreshing.discard(key)


def _store(key: str, plan: "RetrievalPlan") -> None:
    _plans.set(key, plan)
    _loaded_at[key] = time.monotonic()
    if len(_loaded_at) > 4 * _plans._max:  # keep the side table bounded
        for stale in [k for k in _loaded_at if k not in _plans._data]:
            del _loaded_at[stale]


async def _load_prompt_matches(conn: asyncpg.Connection, kb_ids: list[str]) -> list[VectorMatch]:
    """One VectorMatch per prompt-mode document (chunks reassembled); score=1.0 is an "always include" sentinel."""
    if not kb_ids:
        return []
    rows = await conn.fetch(
        """
        SELECT
            (array_agg(c.id ORDER BY c.chunk_index))[1] AS chunk_id,
            c.document_id, c.kb_id,
            string_agg(c.content, ' ' ORDER BY c.chunk_index) AS content,
            sum(coalesce(c.token_count, 0)) AS token_count,
            (array_agg(c.page ORDER BY c.chunk_index))[1] AS page,
            (array_agg(c.language ORDER BY c.chunk_index))[1] AS language,
            (array_agg(c.tags ORDER BY c.chunk_index))[1] AS tags,
            (array_agg(c.version ORDER BY c.chunk_index))[1] AS version,
            d.title AS document_title, kb.slug AS kb_slug
        FROM kb_chunks c
        JOIN kb_documents d ON d.id = c.document_id AND d.deleted_at IS NULL
                            AND d.usage_mode = 'prompt' AND d.status = ANY($2::text[])
        JOIN knowledge_bases kb ON kb.id = c.kb_id
        WHERE c.kb_id = ANY($1::uuid[]) AND NOT c.is_parent
        GROUP BY c.document_id, c.kb_id, d.title, kb.slug
        """,
        kb_ids, SEARCHABLE_STATUSES,
    )
    return [
        VectorMatch(
            chunk_id=str(row["chunk_id"]), document_id=str(row["document_id"]), kb_id=str(row["kb_id"]),
            content=row["content"], score=1.0, token_count=row["token_count"], page=row["page"],
            language=row["language"], tags=json.loads(row["tags"]) if row["tags"] else {},
            version=row["version"], document_title=row["document_title"], kb_slug=row["kb_slug"],
            found_by=("prompt",),
        )
        for row in rows
    ]


async def load_plan(conn: asyncpg.Connection, tenant_slug: str, agent_slug: str) -> RetrievalPlan | None:
    """Resolve from Postgres on `conn` (tenant-scoped, so RLS applies). None = no such agent in this tenant."""
    agent = await conn.fetchrow(
        "SELECT a.id, a.tenant_id, t.slug AS tenant_slug, a.slug AS agent_slug, p.top_k, p.max_tokens, p.minimum_score, p.rerank, p.hybrid_search, "
        "       p.include_citations "
        "FROM agents a JOIN tenants t ON t.id = a.tenant_id "
        "LEFT JOIN agent_retrieval_policies p ON p.agent_id = a.id "
        "WHERE t.slug = $1 AND a.slug = $2 AND a.deleted_at IS NULL AND t.deleted_at IS NULL",
        tenant_slug, agent_slug,
    )
    if agent is None:
        return None
    if agent["tenant_slug"] != tenant_slug or agent["agent_slug"] != agent_slug:
        raise TenantScopeConflict("resolved agent does not belong to the requested tenant/agent slugs")
    kb_rows = await conn.fetch(
        "SELECT kb.id, kb.embedding_config_id, kb.config_version "
        "FROM agent_knowledge_bases akb "
        "JOIN knowledge_bases kb ON kb.id = akb.kb_id AND kb.tenant_id = $2 "
        "AND kb.deleted_at IS NULL AND kb.status = 'active' "
        "WHERE akb.agent_id = $1 AND akb.enabled "
        "ORDER BY kb.id",
        agent["id"], agent["tenant_id"],
    )
    kb_ids = [str(r["id"]) for r in kb_rows]
    config_ids = sorted({str(r["embedding_config_id"]) for r in kb_rows if r["embedding_config_id"] is not None})
    config_rows = await conn.fetch(
        "SELECT * FROM provider_configs WHERE id = ANY($1::uuid[]) AND deleted_at IS NULL", config_ids,
    ) if config_ids else []
    configs = {
        str(r["id"]): EmbeddingProviderConfig(
            id=str(r["id"]), engine=r["engine"], model=r["model"], api_key_ref=r["api_key_ref"],
            extra=json.loads(r["extra"]) if r["extra"] else {},
        )
        for r in config_rows
    }
    groups: dict[str, list[str]] = {}
    degraded: list[str] = []
    for r in kb_rows:
        cfg_id = str(r["embedding_config_id"]) if r["embedding_config_id"] is not None else None
        if cfg_id is None:
            continue  # prompt-mode only KB
        if cfg_id not in configs:
            if "embedding_config_missing" not in degraded:
                degraded.append("embedding_config_missing")
            continue
        groups.setdefault(cfg_id, []).append(str(r["id"]))
    chunk_count = await conn.fetchval(
        "SELECT count(*) FROM kb_chunks WHERE kb_id = ANY($1::uuid[]) AND NOT is_parent", kb_ids,
    ) if kb_ids else 0
    return RetrievalPlan(
        tenant_slug=tenant_slug, agent_slug=agent_slug, chunk_count=chunk_count,
        tenant_id=str(agent["tenant_id"]), agent_id=str(agent["id"]),
        agent_policy=merge_policy(agent, {}),
        kb_ids=kb_ids, groups=groups, embedding_configs=configs,
        prompt_matches=await _load_prompt_matches(conn, kb_ids),
        kb_versions={str(r["id"]): r["config_version"] for r in kb_rows},
        degraded=tuple(degraded),
    )


async def resolve_plan(
    tenant_slug: str, agent_slug: str, *, conn: asyncpg.Connection | None = None, pool: asyncpg.Pool | None = None,
) -> tuple[RetrievalPlan | None, bool]:
    """(plan, was_cached). A found plan is cached; "no such agent" is not, so a caller probing another
    tenant's slug can never plant an empty plan under that tenant's key."""
    hit = cached_plan(tenant_slug, agent_slug)
    if hit is not None:
        return hit, True
    generation = _generation
    if conn is not None:
        plan = await load_plan(conn, tenant_slug, agent_slug)
    else:
        async with tenant_conn(pool or await db.get_pool()) as own_conn:
            plan = await load_plan(own_conn, tenant_slug, agent_slug)
    if plan is not None:
        check_scope(plan)  # cold path too: a plan for another tenant is neither cached nor returned
        if generation == _generation:
            _store(cache.plan_key(tenant_slug, agent_slug), plan)
    return plan, False


async def prewarm(
    tenant_slug: str, agent_slug: str, *,
    embedding_manager: EmbeddingProviderManager | None = None, pool: asyncpg.Pool | None = None,
) -> bool:
    """Resolve and cache the plan, instantiate its embedding providers and open two pooled connections, so the
    first retrieve() of a call spends nothing on setup. Call at call start, after the caller's tenant scope is
    set (set_target_tenant / the router's assert_tenant_access). Returns False when the agent does not exist.
    Never raises for a missing provider: retrieve() degrades, prewarm only tries."""
    pool = pool or await db.get_pool()
    plan, _ = await resolve_plan(tenant_slug, agent_slug, pool=pool)
    if plan is None:
        return False
    if embedding_manager is not None:
        for cfg in plan.embedding_configs.values():
            try:
                await embedding_manager.get(cfg)
            except Exception:  # noqa: BLE001 - a broken provider is retrieve()'s fallback, not prewarm's failure
                pass

    async def _touch() -> None:
        async with pool.acquire() as c:
            await c.fetchval("SELECT 1")

    await asyncio.gather(_touch(), _touch(), return_exceptions=True)
    return True
