"""Re-queue existing documents through the normal ingestion queue, so a chunker change (KNOWLEDGE_CHUNKER, a new
chunker version) reaches documents that are already indexed.

Each document gets its version bumped (as retry_document does) and a new kb_ingestion_jobs row; the worker then
re-parses, re-chunks and swaps the chunks in one transaction, so readers keep the old chunks until the new ones
commit. Everything runs on a tenant-scoped connection under RLS for the one tenant given, never platform_conn:
a tenant's documents cannot be touched from another tenant's call, and the platform operator names the tenant
explicitly. Documents that already have a pending or running job are skipped (no duplicate jobs).

CLI:  python -m services.knowledge.reingest --tenant <uuid> [--kb <uuid>] [--except-version fixed-v1] [--batch 100] [--dry-run]"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

import asyncpg

from libs.tenancy import tenant_conn

from . import db

log = logging.getLogger(__name__)

DEFAULT_BATCH = 100
# Marks the job as background work: _claim_next_job serves every other pending job first, so a tenant-wide
# re-ingest never delays another tenant's (or the same tenant's) fresh uploads.
REINGEST_STAGE = "reingest-queued"

_DUE_SQL = """
    SELECT d.id, d.kb_id FROM kb_documents d
    WHERE d.tenant_id = $1 AND d.deleted_at IS NULL
      AND ($2::uuid IS NULL OR d.kb_id = $2)
      AND d.status IN ('ready', 'ready_degraded')
      AND ($3::text IS NULL OR d.chunker_version IS DISTINCT FROM $3)
      AND NOT EXISTS (SELECT 1 FROM kb_ingestion_jobs j WHERE j.document_id = d.id AND j.status IN ('pending', 'running'))
    ORDER BY d.updated_at, d.id
    LIMIT $4
"""


async def requeue_documents(
    pool: asyncpg.Pool, tenant_id: Any, kb_id: Any | None = None, *, except_version: str | None = None,
    dry_run: bool = False, batch: int | None = DEFAULT_BATCH,
) -> list[str]:
    """Document ids re-queued (or that would be, with dry_run). `except_version` skips documents already at that
    chunker_version, so switching to fixed windows does not redo documents that are already fixed-v1. At most `batch`
    documents per call (oldest-updated first; a re-queued document's updated_at moves it to the back, so repeated
    calls walk through the whole set). The jobs run behind all normal ingestion work (REINGEST_STAGE)."""
    limit = batch if batch and batch > 0 else 10**9
    async with tenant_conn(pool, explicit_tenant=str(tenant_id), reason="kb-ingestion-job") as conn:
        if dry_run:
            rows = await conn.fetch(_DUE_SQL, tenant_id, kb_id, except_version, limit)
            return [str(r["id"]) for r in rows]
        # One re-ingest per tenant at a time: the lock is held to the end of this transaction, and the next statement
        # of a waiting run takes a fresh snapshot, so it sees the jobs this run committed (the NOT EXISTS below is
        # evaluated against the statement snapshot, which alone cannot stop two overlapping runs).
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('kb-reingest:' || $1::text, 0))", str(tenant_id))
        rows = await conn.fetch(
            f"""
            WITH due AS ({_DUE_SQL.replace("LIMIT $4", "LIMIT $4 FOR UPDATE OF d")}),
            bumped AS (
                UPDATE kb_documents d SET version = d.version + 1, updated_at = now()
                FROM due WHERE d.id = due.id RETURNING d.id, d.kb_id
            )
            INSERT INTO kb_ingestion_jobs (document_id, kb_id, stage) SELECT id, kb_id, $5 FROM bumped RETURNING document_id
            """,
            tenant_id, kb_id, except_version, limit, REINGEST_STAGE,
        )
    queued = [str(r["document_id"]) for r in rows]
    log.info("re-queued %d document(s) tenant=%s kb=%s", len(queued), tenant_id, kb_id)
    return queued


async def _main(args: argparse.Namespace) -> int:
    pool = await db.get_pool()
    try:
        ids = await requeue_documents(pool, args.tenant, args.kb, except_version=args.except_version, dry_run=args.dry_run, batch=args.batch)
    finally:
        await db.close_pool()
    print(f"{'would re-queue' if args.dry_run else 're-queued'} {len(ids)} document(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Re-queue indexed documents so a chunker change takes effect")
    p.add_argument("--tenant", required=True, help="tenant uuid (documents of this tenant only)")
    p.add_argument("--kb", help="limit to one knowledge base uuid")
    p.add_argument("--except-version", help="skip documents already at this chunker_version, e.g. fixed-v1")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                   help=f"documents to queue per run (default {DEFAULT_BATCH}); run again for the next batch, 0 = all")
    logging.basicConfig(level="INFO")
    return asyncio.run(_main(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
