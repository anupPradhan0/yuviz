"""Move a knowledge base to another embedding config (model) without downtime.

    python -m services.knowledge.reembed --tenant <uuid> --kb <uuid> --to-config <uuid> [--batch N] [--dry-run]
    python -m services.knowledge.reembed --tenant <uuid> --kb <uuid> --abandon      # drop a half-done migration

How it works. kb_chunks.embedding_next / embedding_next_model are a shadow copy, filled in bounded batches with the
NEW config. Queries never read them: the KB keeps its old embedding_config_id and `embedding` the whole time, so
nothing is searched in a mixed vector space. When every embedded chunk has a shadow vector made by the target model,
ONE transaction (holding the KB row lock) moves embedding_next -> embedding, sets embedding_model and the KB's
embedding_config_id, and clears the shadow. A reader sees all-old or all-new, never a mix.

Resumable: a chunk is pending until its shadow vector exists AND was made by the target model, so re-running
continues where the last run stopped, and switching targets re-embeds what the old target had done.

Chunks added or changed while it runs (uploads, re-ingests) are embedded by the ingestion worker with the KB's OLD
config (the KB row still points at it), so they have no shadow vector and simply become pending again; the swap
re-checks that under the KB row lock and refuses if any are left (the loop then embeds them and tries again). The
worker's index step takes FOR SHARE on the KB row and refuses to commit vectors made with a config the KB no longer
has (assert_kb_config_current), so a worker that started before the swap cannot land old-space vectors after it.

Boundaries. Chunk text is embedded as stored: contextual enrichment (enrichment.py) is NOT re-applied, re-ingest the
documents for that. Cross-process plan caches (the service's retrieval plan, up to KNOWLEDGE_PLAN_TTL_S, 30 s by
default) learn of the swap on their next refresh; until then a stale plan embeds queries with the old model, which
finds little in the new space (lexical search still answers). Every statement is tenant-scoped (tenant_conn with
explicit tenant, reason 'kb-ingestion-job'); chunk text goes only to the target config's own embedding provider."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import asyncpg

from libs.tenancy import tenant_conn

from . import audit, db
from . import retrieval_plan as plans
from .embedding_manager import (
    EMBEDDING_DIMS, EmbeddingProviderConfig, EmbeddingProviderManager, effective_model, embed_in_batches,
)
from .secret_resolver import CompositeSecretResolver

log = logging.getLogger(__name__)

_REASON = "kb-ingestion-job"
DEFAULT_BATCH = 64
MAX_SWAP_ROUNDS = 5  # fill -> swap refused (new chunks arrived) -> fill again, this many times at most

# What still has to be embedded: real chunks (parents and inline prompt docs have no vector) without a shadow vector
# made by the target model.
_PENDING = (
    "kb_id = $1 AND NOT is_parent AND embedding IS NOT NULL "
    "AND (embedding_next IS NULL OR embedding_next_model IS DISTINCT FROM $2)"
)


class KbConfigChanged(RuntimeError):
    """Transient (RuntimeError): the KB was migrated while a worker was embedding; the retry uses the new config."""


async def assert_kb_config_current(conn: asyncpg.Connection, kb_id: Any, embedded_with: str) -> None:
    """For the ingestion worker's index transaction. FOR SHARE makes a concurrent swap wait for this commit, and
    makes this check see a swap that already committed."""
    current = await conn.fetchval("SELECT embedding_config_id FROM knowledge_bases WHERE id = $1 FOR SHARE", kb_id)
    if current is None or str(current) != str(embedded_with):
        raise KbConfigChanged("the knowledge base moved to another embedding config while this document was embedded")


@dataclass
class ReembedReport:
    total: int = 0          # chunks that need a vector in the new space
    already_done: int = 0   # had a shadow vector from a previous run
    embedded: int = 0       # embedded by this run
    batches: int = 0
    swapped: bool = False
    reason: str = ""        # why it did not swap: dry_run | stopped | incomplete | already_on_target


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


async def _load(conn: asyncpg.Connection, kb_id: Any, to_config: Any) -> tuple[dict, EmbeddingProviderConfig]:
    kb = await conn.fetchrow(
        "SELECT id, tenant_id, embedding_config_id FROM knowledge_bases WHERE id = $1 AND deleted_at IS NULL", kb_id)
    if kb is None:
        raise ValueError(f"knowledge base {kb_id} not found for this tenant")
    row = await conn.fetchrow(
        "SELECT * FROM provider_configs WHERE id = $1 AND role = 'embedding' AND deleted_at IS NULL", to_config)
    if row is None:
        raise ValueError(f"embedding config {to_config} not found for this tenant")
    extra = json.loads(row["extra"]) if row["extra"] else {}
    cfg = EmbeddingProviderConfig(
        id=str(row["id"]), engine=row["engine"], model=row["model"], api_key_ref=row["api_key_ref"], extra=extra)
    return dict(kb), cfg


async def _write_batch(
    pool: asyncpg.Pool, tenant_id: str, kb_id: Any, model: str, ids: list[Any], vectors: list[list[float]],
) -> None:
    rows = sorted(zip(ids, vectors), key=lambda p: str(p[0]))  # one global lock order: no deadlock with other batches
    for attempt in range(3):
        try:
            async with tenant_conn(pool, explicit_tenant=tenant_id, reason=_REASON) as conn:
                # A chunk re-ingested meanwhile is gone (new ids), so its UPDATE touches nothing; its replacement
                # is pending on the next pass. Text never changes under an id, so a vector can never be stale.
                await conn.executemany(
                    "UPDATE kb_chunks SET embedding_next = $2::vector, embedding_next_model = $3 "
                    "WHERE id = $1 AND kb_id = $4 AND embedding IS NOT NULL",
                    [(cid, _literal(v), model, kb_id) for cid, v in rows],
                )
            return
        except asyncpg.DeadlockDetectedError:
            if attempt == 2:
                raise
            await asyncio.sleep(0.05 * (attempt + 1))


async def swap(pool: asyncpg.Pool, tenant_id: str, kb_id: Any, cfg: EmbeddingProviderConfig) -> tuple[bool, str]:
    """The atomic step. Returns (swapped, reason)."""
    model = effective_model(cfg)
    async with tenant_conn(pool, explicit_tenant=tenant_id, reason=_REASON) as conn:
        kb = await conn.fetchrow(
            "SELECT embedding_config_id FROM knowledge_bases WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", kb_id)
        if kb is None:
            raise ValueError(f"knowledge base {kb_id} not found for this tenant")
        if str(kb["embedding_config_id"]) == cfg.id:
            return False, "already_on_target"
        # Under the lock: in-flight ingests have committed (or will fail assert_kb_config_current), so this count is final.
        pending = await conn.fetchval(f"SELECT count(*) FROM kb_chunks WHERE {_PENDING}", kb_id, model)
        if pending:
            return False, "incomplete"
        live = await conn.fetchval("SELECT 1 FROM provider_configs WHERE id = $1 AND deleted_at IS NULL", cfg.id)
        if live is None:
            raise ValueError("the target embedding config was deleted during the migration")
        await conn.execute(
            "UPDATE kb_chunks SET embedding = embedding_next, embedding_model = embedding_next_model, dims = $2, "
            "embedding_next = NULL, embedding_next_model = NULL WHERE kb_id = $1 AND embedding_next IS NOT NULL",
            kb_id, EMBEDDING_DIMS,
        )
        await conn.execute("UPDATE knowledge_bases SET embedding_config_id = $2 WHERE id = $1", kb_id, cfg.id)
        await audit.write_audit(
            conn, entity_type="knowledge_base", entity_id=kb_id, action="updated",
            old_value={"embedding_config_id": str(kb["embedding_config_id"])}, new_value={"embedding_config_id": cfg.id},
        )
    plans.invalidate_tenant_id(tenant_id)  # this process only; other processes: see the module docstring
    return True, ""


async def abandon(pool: asyncpg.Pool, tenant_id: str, kb_id: Any) -> int:
    """Drop a half-done migration: the KB was never changed, so this only clears the shadow columns."""
    async with tenant_conn(pool, explicit_tenant=tenant_id, reason=_REASON) as conn:
        status = await conn.execute(
            "UPDATE kb_chunks SET embedding_next = NULL, embedding_next_model = NULL "
            "WHERE kb_id = $1 AND (embedding_next IS NOT NULL OR embedding_next_model IS NOT NULL)", kb_id)
    return int(status.split()[-1])


async def reembed_kb(
    pool: asyncpg.Pool, manager: EmbeddingProviderManager, *, tenant_id: str, kb_id: Any, to_config: Any,
    batch: int = DEFAULT_BATCH, dry_run: bool = False, max_batches: int | None = None,
    progress: Callable[[ReembedReport], None] | None = None,
) -> ReembedReport:
    """Fill the shadow column and swap. `max_batches` stops early without swapping (tests, throttled runs)."""
    async with tenant_conn(pool, explicit_tenant=tenant_id, reason=_REASON) as conn:
        kb, cfg = await _load(conn, kb_id, to_config)
        model = effective_model(cfg)
        report = ReembedReport()
        report.total = await conn.fetchval(
            "SELECT count(*) FROM kb_chunks WHERE kb_id = $1 AND NOT is_parent AND embedding IS NOT NULL", kb_id)
        pending = await conn.fetchval(f"SELECT count(*) FROM kb_chunks WHERE {_PENDING}", kb_id, model)
        report.already_done = report.total - pending
    if str(kb["embedding_config_id"]) == cfg.id:
        report.reason = "already_on_target"
        return report
    if dry_run:
        report.reason = "dry_run"
        return report

    provider = await manager.get(cfg)
    for _round in range(MAX_SWAP_ROUNDS):
        while True:
            if max_batches is not None and report.batches >= max_batches:
                report.reason = "stopped"
                return report
            async with tenant_conn(pool, explicit_tenant=tenant_id, reason=_REASON) as conn:
                rows = await conn.fetch(
                    f"SELECT id, content FROM kb_chunks WHERE {_PENDING} ORDER BY id LIMIT $3", kb_id, model, batch)
            if not rows:
                break
            # No connection is held while the provider works. embed_in_batches validates count, 768 dims, finite, non-zero.
            vectors = await embed_in_batches(provider, [r["content"] for r in rows])
            await _write_batch(pool, tenant_id, kb_id, model, [r["id"] for r in rows], vectors)
            report.batches += 1
            report.embedded += len(rows)
            if progress:
                progress(report)
        report.swapped, report.reason = await swap(pool, tenant_id, kb_id, cfg)
        if report.swapped or report.reason != "incomplete":
            break
    return report


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Re-embed a knowledge base with another embedding config, then swap.")
    p.add_argument("--tenant", required=True, help="tenant UUID")
    p.add_argument("--kb", required=True, help="knowledge base UUID")
    p.add_argument("--to-config", help="provider_configs UUID (role 'embedding') to migrate to")
    p.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    p.add_argument("--dry-run", action="store_true", help="count what would be embedded; call no provider")
    p.add_argument("--abandon", action="store_true", help="clear a half-done migration's shadow vectors and stop")
    args = p.parse_args(argv)
    if not args.abandon and not args.to_config:
        p.error("--to-config is required unless --abandon")
    return args


async def _main(args: argparse.Namespace) -> int:
    pool = await db.get_pool()
    try:
        if args.abandon:
            print(f"cleared shadow vectors on {await abandon(pool, args.tenant, args.kb)} chunks")
            return 0
        manager = EmbeddingProviderManager(CompositeSecretResolver())
        report = await reembed_kb(
            pool, manager, tenant_id=args.tenant, kb_id=args.kb, to_config=args.to_config,
            batch=max(1, args.batch), dry_run=args.dry_run,
            progress=lambda r: print(f"embedded {r.already_done + r.embedded}/{r.total} chunks", flush=True),
        )
        print(f"total={report.total} already_done={report.already_done} embedded={report.embedded} "
              f"swapped={report.swapped} {('reason=' + report.reason) if report.reason else ''}")
        return 0 if (report.swapped or report.reason in ("dry_run", "already_on_target")) else 1
    finally:
        await db.close_pool()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    sys.exit(asyncio.run(_main(_parse(argv))))


if __name__ == "__main__":
    main()
