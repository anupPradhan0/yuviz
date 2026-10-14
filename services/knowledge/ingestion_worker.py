"""Ingestion worker: claims kb_ingestion_jobs; read -> extract -> chunk -> embed -> kb_chunks.
Small docs (< AUTO_INLINE_THRESHOLD_BYTES) skip embedding and become usage_mode='prompt'.
Parsing detects the type from the bytes and runs in a sandboxed subprocess (parsers/); a document ends in
ready, ready_degraded (indexed, but part of it was unreadable) or failed with a plain reason.

Queue semantics (many workers are safe):
  - claim is one UPDATE ... FOR UPDATE SKIP LOCKED, with a soft per-tenant running cap
  - a running job holds a lease that a heartbeat extends; reap_expired_leases() requeues a dead worker's job
  - permanent errors (ValueError, missing file) fail at once; anything else retries with backoff,
    then dead-letters after max_attempts. Retrying a dead-lettered document goes through retry_document()
  - final writes are fenced on (status='running', attempts) so a stale worker cannot overwrite a newer one
Job claim and lease reaping use platform_conn(); all later statements re-scope to the document's tenant."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from libs.tenancy import platform_conn, tenant_conn

from . import db
from .chunker import ChunkConfig, chunk_document, chunker_version
from .chunking import Chunk, count_tokens
from .embedding_manager import (
    EMBEDDING_DIMS, EmbeddingProviderConfig, EmbeddingProviderManager, effective_model, embed_in_batches,
    provider_error,
)
from .enrichment import enrich_texts
from .parsers import SUPPORTED_CONTENT_TYPES, ParsedDocument, ParseRejected, parse_document
from .reembed import assert_kb_config_current
from .storage import StorageProvider

log = logging.getLogger(__name__)

AUTO_INLINE_THRESHOLD_BYTES = 500
POLL_INTERVAL_S = 2.0

LEASE_SECONDS = 120.0
HEARTBEAT_SECONDS = 30.0
REAP_INTERVAL_S = 15.0
BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 300.0
DEFAULT_MAX_ATTEMPTS = 5
# Soft cap: W workers claiming for one tenant at the same instant can overshoot it by up to W-1.
# It only bites with several worker processes; one run_loop runs one job at a time.
PER_TENANT_RUNNING_CAP = int(os.environ.get("KB_INGEST_PER_TENANT_CAP", "2"))

_PERMANENT_ERRORS = (ValueError, FileNotFoundError)

# A ready document keeps its status (and its old chunks stay searchable) through a failed, retried, dead-lettered or
# reaped RE-ingest; first-time ingests keep pending -> processing -> ready/failed.
REINDEX_FAILED_PREFIX = "re-index failed: "


FAILED_SQL, PENDING_SQL = "'failed'", "'pending'"


def _keep_serving(new: str) -> str:
    return f"CASE WHEN status IN ('ready', 'ready_degraded') THEN status ELSE {new} END"


def classify_error(exc: BaseException) -> str:
    """'permanent' = retrying cannot help (bad type, empty doc, missing config/file); else 'transient'."""
    coded = provider_error(exc)
    if coded is not None:
        exc = coded  # decided from the structured exception, not from its text
    return "permanent" if isinstance(exc, _PERMANENT_ERRORS) else "transient"


_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def _scrub_url(match: re.Match) -> str:
    parts = urlsplit(match.group(0))
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def stored_error(exc: BaseException) -> str:
    """What goes in the document/job error column: a parse rejection's reason is already plain and tenant-safe."""
    if isinstance(exc, ParseRejected):
        return exc.reason
    # A provider call goes to a tenant-chosen URL; its failure is a fixed code, never the exception text (read-back oracle).
    coded = provider_error(exc)
    return coded.code if coded is not None else redact_error(exc)


def redact_error(exc: BaseException) -> str:
    """Exception type + message with URL credentials and query strings removed.

    Stored in document/job/event rows that other members of the tenant can read, and provider
    errors (httpx) embed the request URL, which can carry user:password@ or a key in the query."""
    return f"{type(exc).__name__}: {_URL_RE.sub(_scrub_url, str(exc))}"[:1000]


def backoff_seconds(attempt: int, rand: float | None = None) -> float:
    """Delay before retry number `attempt` (1-based): exponential, capped, +/-20% jitter."""
    base = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** max(0, attempt - 1)))
    r = random.random() if rand is None else rand
    return base * (0.8 + 0.4 * r)


class _StageLog:
    """Collects per-stage events in memory; flushed in the same transaction as the job's final state."""

    def __init__(self, attempt: int) -> None:
        self.attempt = attempt
        self.events: list[dict[str, Any]] = []
        self.stage = "read"
        self._t0 = time.monotonic()

    def begin(self, stage: str) -> None:
        self.stage = stage
        self._t0 = time.monotonic()

    def ok(self, **detail: Any) -> None:
        self._add("ok", detail)

    def error(self, exc: BaseException) -> None:
        self._add("error", {"error_class": classify_error(exc), "error": stored_error(exc)})

    def add(self, stage: str, status: str, **detail: Any) -> None:
        self.events.append({"stage": stage, "status": status, "duration_ms": None, "detail": detail})

    def _add(self, status: str, detail: dict[str, Any]) -> None:
        self.events.append({
            "stage": self.stage, "status": status,
            "duration_ms": int((time.monotonic() - self._t0) * 1000), "detail": detail,
        })


async def _write_events(
    conn: asyncpg.Connection, job: dict, tenant_id: Any, events: list[dict[str, Any]],
) -> None:
    for e in events:
        await conn.execute(
            "INSERT INTO kb_ingestion_events (job_id, document_id, tenant_id, attempt, stage, status, duration_ms, detail) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)",
            job["id"], job["document_id"], tenant_id, job["attempts"],
            e["stage"], e["status"], e["duration_ms"], json.dumps(e["detail"]),
        )


async def _fetch_embedding_config(conn: asyncpg.Connection, kb_id) -> EmbeddingProviderConfig:
    row = await conn.fetchrow(
        "SELECT pc.* FROM knowledge_bases kb "
        "JOIN provider_configs pc ON pc.id = kb.embedding_config_id "
        "WHERE kb.id = $1 AND pc.deleted_at IS NULL",
        kb_id,
    )
    if row is None:
        raise ValueError(f"knowledge_base {kb_id} has no embedding_config_id configured")
    extra = json.loads(row["extra"]) if row["extra"] else {}
    return EmbeddingProviderConfig(
        id=str(row["id"]), engine=row["engine"], model=row["model"],
        api_key_ref=row["api_key_ref"], extra=extra,
    )


async def _extract(doc_row: Any, raw: bytes) -> ParsedDocument:
    """The stored content_type is only a hint (text/markdown vs text/plain); the bytes decide the parser."""
    return await parse_document(raw, hint=doc_row["content_type"], language=doc_row["language"])


_MARK_RUNNING_SQL = (
    "UPDATE kb_ingestion_jobs SET status = 'running', started_at = now(), updated_at = now(), "
    "attempts = attempts + 1, stage = 'claimed', "
    "lease_expires_at = now() + make_interval(secs => $2) "
    "WHERE id = $1 RETURNING *"
)


async def _heartbeat(
    pool: asyncpg.Pool, job_id: Any, tenant_id: str, attempt: int, lease_s: float, interval_s: float,
) -> None:
    """Extends this attempt's lease; stops once the job belongs to another attempt (its writes are fenced)."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            async with tenant_conn(pool, explicit_tenant=tenant_id, reason="kb-ingestion-job") as conn:
                status = await conn.execute(
                    "UPDATE kb_ingestion_jobs SET lease_expires_at = now() + make_interval(secs => $2), "
                    "updated_at = now() WHERE id = $1 AND status = 'running' AND attempts = $3",
                    job_id, lease_s, attempt,
                )
            if status == "UPDATE 0":
                log.warning("heartbeat job=%s attempt=%s: job no longer owned by this attempt; stopping", job_id, attempt)
                return
        except Exception:
            log.warning("heartbeat failed job=%s; the reaper requeues the job if the lease lapses", job_id, exc_info=True)


async def _still_owns_job(conn: asyncpg.Connection, job: dict) -> bool:
    """Row-locks the job and checks this worker's attempt still owns it (not reaped and reclaimed)."""
    row = await conn.fetchrow(
        "SELECT 1 FROM kb_ingestion_jobs WHERE id = $1 AND status = 'running' AND attempts = $2 FOR UPDATE",
        job["id"], job["attempts"],
    )
    return row is not None


async def process_one_job(
    pool: asyncpg.Pool,
    storage: StorageProvider,
    embedding_manager: EmbeddingProviderManager,
    job: dict,
    *,
    lease_s: float = LEASE_SECONDS,
    heartbeat_s: float = HEARTBEAT_SECONDS,
) -> None:
    document_id, kb_id = job["document_id"], job["kb_id"]

    async with platform_conn(pool, reason="kb-ingestion-job-claim") as conn:
        if job.get("status") != "running":  # called directly rather than through _claim_next_job
            job = dict(await conn.fetchrow(_MARK_RUNNING_SQL, job["id"], lease_s))
        doc_row = await conn.fetchrow("SELECT * FROM kb_documents WHERE id = $1", document_id)
        if doc_row is not None:
            # A document that is already ready keeps serving its old chunks while it re-ingests: only a first-time
            # (or retried-after-failure) ingest shows as 'processing'.
            await conn.execute(
                "UPDATE kb_documents SET status = 'processing' WHERE id = $1 AND status NOT IN ('ready', 'ready_degraded')",
                document_id,
            )
        else:
            await conn.execute(
                "UPDATE kb_ingestion_jobs SET status = 'failed', error_class = 'permanent', "
                "error = 'document not found', finished_at = now(), lease_expires_at = NULL WHERE id = $1",
                job["id"],
            )
    if doc_row is None:
        log.error("Ingestion job=%s references missing document=%s", job["id"], document_id)
        return
    tenant_id = str(doc_row["tenant_id"])

    stages = _StageLog(job["attempts"])
    heartbeat = asyncio.create_task(_heartbeat(pool, job["id"], tenant_id, job["attempts"], lease_s, heartbeat_s))
    try:
        stages.begin("read")
        raw = await storage.read(doc_row["source_ref"])
        stages.ok(bytes=len(raw))

        stages.begin("extract")
        parsed = await _extract(doc_row, raw)
        text = parsed.text()
        stages.ok(
            chars=len(text), detected_type=parsed.detected_type, parser=parsed.parser_used,
            quality=parsed.quality_score, degraded=parsed.degraded, warnings=parsed.warnings[:5],
            hidden_removed=parsed.hidden_removed,
        )

        stages.begin("chunk")
        parents: list[Chunk] = []
        strategy = "inline"
        embedding_model: str | None = None
        inline = False
        first_ingest = doc_row["status"] not in ("ready", "ready_degraded")  # read before the claim flipped it
        if len(raw) < AUTO_INLINE_THRESHOLD_BYTES:
            new_chunks: list[Chunk] = [Chunk(content=text, token_count=count_tokens(text, doc_row["language"]))]
            vectors: list[list[float] | None] = [None]
            inline = True
            stages.ok(chunks=1, inline=True)
        else:
            try:  # the model's token limit sizes the chunks; a missing config is reported by the embed stage below
                async with tenant_conn(pool, explicit_tenant=tenant_id, reason="kb-ingestion-job") as conn:
                    embedding_cfg: EmbeddingProviderConfig | None = await _fetch_embedding_config(conn, kb_id)
            except ValueError:
                embedding_cfg = None
            max_tokens = embedding_cfg.extra.get("max_tokens") if embedding_cfg else None
            # Off the event loop: chunking is CPU work, and the heartbeat must keep running while it does. It carries its
            # own wall-clock budget and chunk ceiling (KNOWLEDGE_CHUNK_BUDGET_S, KNOWLEDGE_MAX_CHUNKS_PER_DOC) and
            # raises a permanent error when either is hit, before anything is embedded.
            result = await asyncio.to_thread(
                chunk_document, parsed.blocks, doc_row["language"], config=ChunkConfig.for_embedder(max_tokens),
            )
            new_chunks, parents, strategy = result.children, result.parents, result.strategy
            if not new_chunks:
                raise ValueError("document produced zero chunks (empty or unextractable content)")
            stages.ok(chunks=len(new_chunks), parents=len(parents), strategy=strategy, inline=False)

            stages.begin("embed")
            if embedding_cfg is None:
                async with tenant_conn(pool, explicit_tenant=tenant_id, reason="kb-ingestion-job") as conn:
                    embedding_cfg = await _fetch_embedding_config(conn, kb_id)
            provider = await embedding_manager.get(embedding_cfg)
            embed_stats: dict[str, int] = {}
            # Optional contextual enrichment (off by default): only the EMBEDDED text changes, never the stored chunk.
            embed_texts, enrich_detail = await enrich_texts(
                pool, tenant_id, doc_row["title"], new_chunks, embed_max_tokens=embedding_cfg.extra.get("max_tokens"),
                attempt=job["attempts"])
            vectors = await embed_in_batches(provider, embed_texts, stats=embed_stats)
            embedding_model = effective_model(embedding_cfg)
            stages.ok(vectors=len(vectors), **embed_stats, **enrich_detail)

        stages.begin("index")
        async with tenant_conn(pool, explicit_tenant=tenant_id, reason="kb-ingestion-job") as conn:
            if not await _still_owns_job(conn, job):
                log.warning("Ingestion job=%s attempt=%s lost its lease; dropping results", job["id"], job["attempts"])
                return
            if embedding_model is not None:  # re-embed migration: never land vectors from a config the KB just left
                await assert_kb_config_current(conn, kb_id, embedding_cfg.id, doc_row["tenant_id"])
            # The document row as it is NOW, locked: a ready document stays editable while it re-ingests, so an admin may
            # have changed usage_mode, language or title (or deleted it) since this job claimed it. Those edits win; only
            # the tiny-document auto-inline decision of a FIRST ingest may set 'prompt'.
            cur = await conn.fetchrow(
                "SELECT usage_mode, language, version, deleted_at FROM kb_documents WHERE id = $1 FOR UPDATE", document_id,
            )
            if cur is None or cur["deleted_at"] is not None:
                log.warning("Ingestion job=%s: document=%s was deleted while it was being processed; not indexing", job["id"], document_id)
                stages.add("index", "error", error_class="permanent", error="document was deleted during ingestion")
                await _write_events(conn, job, doc_row["tenant_id"], stages.events)
                await conn.execute(
                    "UPDATE kb_ingestion_jobs SET status = 'failed', error = 'document was deleted during ingestion', "
                    "error_class = 'permanent', stage = 'index', lease_expires_at = NULL, finished_at = now(), "
                    "updated_at = now() WHERE id = $1", job["id"],
                )
                return
            language = cur["language"]
            usage_mode = "prompt" if inline and first_ingest else cur["usage_mode"]
            # The queuer owns the version (retry_document bumps it); this swap is one transaction,
            # so readers see the old chunks until commit and the new ones after, never a gap.
            version = cur["version"]
            await conn.execute("DELETE FROM kb_chunks WHERE document_id = $1", document_id)
            # Parents first (chunk_index = -1 - ordinal, never colliding with a child's index); children point at them.
            parent_ids: list[Any] = []
            for ordinal, parent in enumerate(parents):
                parent_ids.append(await conn.fetchval(
                    "INSERT INTO kb_chunks "
                    "(document_id, kb_id, tenant_id, chunk_index, content, embedding, token_count, page, language, "
                    " version, is_parent, section_path) "
                    "VALUES ($1, $2, $3, $4, $5, NULL, $6, $7, $8, $9, true, $10) RETURNING id",
                    document_id, kb_id, doc_row["tenant_id"], -1 - ordinal, parent.content, parent.token_count,
                    parent.page, language, version, parent.section_path,
                ))
            for i, (chunk, vector) in enumerate(zip(new_chunks, vectors)):
                vector_literal = None if vector is None else "[" + ",".join(repr(float(x)) for x in vector) + "]"
                await conn.execute(
                    "INSERT INTO kb_chunks "
                    "(document_id, kb_id, tenant_id, chunk_index, content, embedding, token_count, page, language, "
                    " version, parent_id, section_path, embedding_model, dims) "
                    "VALUES ($1, $2, $3, $4, $5, $6::vector, $7, $8, $9, $10, $11, $12, $13, $14)",
                    document_id, kb_id, doc_row["tenant_id"], i, chunk.content, vector_literal, chunk.token_count,
                    chunk.page, language, version,
                    None if chunk.parent is None else parent_ids[chunk.parent], chunk.section_path,
                    embedding_model if vector is not None else None, EMBEDDING_DIMS if vector is not None else None,
                )
            # ready_degraded keeps its reason in `error` so the console can say what was unreadable.
            await conn.execute(
                "UPDATE kb_documents SET status = $4, error = $5, version = $2, usage_mode = $3, "
                "parser_used = $6, quality_score = $7, chunker_version = $8, updated_at = now() WHERE id = $1",
                document_id, version, usage_mode,
                "ready_degraded" if parsed.degraded else "ready",
                "; ".join(parsed.warnings)[:1000] if parsed.degraded else None,
                parsed.parser_used, parsed.quality_score, chunker_version(strategy) if strategy != "inline" else "inline",
            )
            stages.ok(chunks=len(new_chunks))
            await _write_events(conn, job, doc_row["tenant_id"], stages.events)
            await conn.execute(
                "UPDATE kb_ingestion_jobs SET status = 'succeeded', stage = 'done', error = NULL, "
                "error_class = NULL, lease_expires_at = NULL, finished_at = now(), updated_at = now() WHERE id = $1",
                job["id"],
            )
        log.info("Ingested document=%s kb=%s chunks=%d usage_mode=%s", document_id, kb_id, len(new_chunks), usage_mode)

    except Exception as exc:
        log.exception("Ingestion failed document=%s kb=%s stage=%s attempt=%s", document_id, kb_id, stages.stage, job["attempts"])
        stages.error(exc)
        await _record_failure(pool, job, doc_row, tenant_id, stages, exc)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def _record_failure(
    pool: asyncpg.Pool, job: dict, doc_row: Any, tenant_id: str, stages: _StageLog, exc: BaseException,
) -> None:
    """Permanent -> failed; transient -> pending with backoff; transient out of attempts -> dead_letter."""
    klass = classify_error(exc)
    attempts, max_attempts = job["attempts"], job.get("max_attempts") or DEFAULT_MAX_ATTEMPTS
    error = stored_error(exc)
    if klass == "permanent":
        job_status, doc_status, delay = "failed", "failed", None
    elif attempts >= max_attempts:
        job_status, doc_status, delay = "dead_letter", "failed", None
        stages.add("worker", "dead_letter", attempts=attempts, max_attempts=max_attempts)
    else:
        job_status, doc_status, delay = "pending", "pending", backoff_seconds(attempts)
        stages.add("worker", "retry_scheduled", attempts=attempts, retry_in_s=round(delay, 1))

    async with tenant_conn(pool, explicit_tenant=tenant_id, reason="kb-ingestion-job") as conn:
        if not await _still_owns_job(conn, job):
            log.warning("Ingestion job=%s attempt=%s lost its lease; dropping failure", job["id"], job["attempts"])
            return
        await _write_events(conn, job, doc_row["tenant_id"], stages.events)
        # A failed RE-ingest must not take a ready document offline: it keeps its status and old chunks, and the
        # reason is recorded on the document (the console shows it) and in the job and its events.
        await conn.execute(
            f"UPDATE kb_documents SET status = {_keep_serving('$2')}, "
            f"error = CASE WHEN status IN ('ready', 'ready_degraded') THEN $4 || $3 ELSE $3 END WHERE id = $1",
            doc_row["id"], doc_status, error, REINDEX_FAILED_PREFIX,
        )
        await conn.execute(
            "UPDATE kb_ingestion_jobs SET status = $2, error = $3, error_class = $4, stage = $5, "
            "lease_expires_at = NULL, updated_at = now(), "
            "finished_at = CASE WHEN $2 = 'pending' THEN NULL ELSE now() END, "
            "next_attempt_at = CASE WHEN $6::float8 IS NULL THEN next_attempt_at "
            "                       ELSE now() + make_interval(secs => $6::float8) END "
            "WHERE id = $1",
            job["id"], job_status, error, klass, stages.stage, delay,
        )


async def _claim_next_job(
    pool: asyncpg.Pool, lease_s: float = LEASE_SECONDS, tenant_cap: int | None = None,
) -> dict | None:
    """Claim one due pending job (SKIP LOCKED, so concurrent workers never take the same one)."""
    cap = PER_TENANT_RUNNING_CAP if tenant_cap is None else tenant_cap
    async with platform_conn(pool, reason="kb-ingestion-job-claim") as conn:
        row = await conn.fetchrow(
            """
            WITH running AS (
                SELECT rd.tenant_id, count(*) AS n FROM kb_ingestion_jobs r
                JOIN kb_documents rd ON rd.id = r.document_id
                WHERE r.status = 'running' GROUP BY rd.tenant_id
            )
            UPDATE kb_ingestion_jobs j
               SET status = 'running', started_at = now(), updated_at = now(), attempts = j.attempts + 1,
                   stage = 'claimed', lease_expires_at = now() + make_interval(secs => $1)
             WHERE j.id = (
                 SELECT c.id FROM kb_ingestion_jobs c
                 JOIN kb_documents d ON d.id = c.document_id AND d.deleted_at IS NULL
                 LEFT JOIN running ru ON ru.tenant_id = d.tenant_id
                 WHERE c.status = 'pending' AND c.next_attempt_at <= now()
                   AND COALESCE(ru.n, 0) < $2
                 ORDER BY c.background, c.next_attempt_at, c.created_at
                 FOR UPDATE OF c SKIP LOCKED
                 LIMIT 1)
            RETURNING j.*
            """,
            lease_s, cap,
        )
    return dict(row) if row is not None else None


async def _cancel_jobs_of_deleted_documents(conn: asyncpg.Connection) -> int:
    """A pending job whose document was soft-deleted is never claimed (the claim skips deleted documents) and would sit
    in the queue forever, counted by the health endpoint and the stalled-queue alert. One short UPDATE (no row lock on
    the document or the tenant) marks it failed/permanent with an event."""
    rows = await conn.fetch(
        "UPDATE kb_ingestion_jobs j SET status = 'failed', error = 'document was deleted', error_class = 'permanent', "
        "finished_at = now(), updated_at = now(), lease_expires_at = NULL "
        "FROM kb_documents d WHERE d.id = j.document_id AND d.deleted_at IS NOT NULL AND j.status = 'pending' "
        "RETURNING j.id, j.document_id, j.attempts, d.tenant_id",
    )
    for r in rows:
        await conn.execute(
            "INSERT INTO kb_ingestion_events (job_id, document_id, tenant_id, attempt, stage, status, detail) "
            "VALUES ($1, $2, $3, $4, 'worker', 'error', $5::jsonb)",
            r["id"], r["document_id"], r["tenant_id"], r["attempts"],
            json.dumps({"error_class": "permanent", "error": "document was deleted"}),
        )
    if rows:
        log.info("Cancelled %d pending ingestion job(s) of deleted documents", len(rows))
    return len(rows)


async def reap_expired_leases(pool: asyncpg.Pool) -> int:
    """Requeue (or dead-letter) running jobs whose worker stopped heartbeating. Returns jobs reaped."""
    reaped = 0
    async with platform_conn(pool, reason="kb-ingestion-lease-reaper") as conn:
        await _cancel_jobs_of_deleted_documents(conn)
        rows = await conn.fetch(
            "SELECT j.*, d.tenant_id AS doc_tenant_id FROM kb_ingestion_jobs j "
            "JOIN kb_documents d ON d.id = j.document_id "
            "WHERE j.status = 'running' AND ("
            "  j.lease_expires_at < now() "
            "  OR (j.lease_expires_at IS NULL AND j.started_at < now() - make_interval(secs => $1))) "
            "FOR UPDATE OF j SKIP LOCKED",
            LEASE_SECONDS,
        )
        for row in rows:
            attempts, max_attempts = row["attempts"], row["max_attempts"]
            exhausted = attempts >= max_attempts
            message = f"worker lease expired during {row['stage'] or 'processing'}"
            if exhausted:
                await conn.execute(
                    "UPDATE kb_ingestion_jobs SET status = 'dead_letter', error = $2, error_class = 'transient', "
                    "lease_expires_at = NULL, finished_at = now(), updated_at = now() WHERE id = $1",
                    row["id"], message,
                )
                await conn.execute(
                    f"UPDATE kb_documents SET status = {_keep_serving(FAILED_SQL)}, "
                    "error = CASE WHEN status IN ('ready', 'ready_degraded') THEN $3 || $2 ELSE $2 END WHERE id = $1",
                    row["document_id"], message, REINDEX_FAILED_PREFIX,
                )
                event_status = "dead_letter"
            else:
                delay = backoff_seconds(attempts)
                await conn.execute(
                    "UPDATE kb_ingestion_jobs SET status = 'pending', error = $2, error_class = 'transient', "
                    "lease_expires_at = NULL, updated_at = now(), "
                    "next_attempt_at = now() + make_interval(secs => $3::float8) WHERE id = $1",
                    row["id"], message, delay,
                )
                await conn.execute(
                    f"UPDATE kb_documents SET status = {_keep_serving(PENDING_SQL)}, "
                    "error = CASE WHEN status IN ('ready', 'ready_degraded') THEN $3 || $2 ELSE $2 END WHERE id = $1",
                    row["document_id"], message, REINDEX_FAILED_PREFIX,
                )
                event_status = "lease_expired"
            await conn.execute(
                "INSERT INTO kb_ingestion_events (job_id, document_id, tenant_id, attempt, stage, status, detail) "
                "VALUES ($1, $2, $3, $4, 'worker', $5, $6::jsonb)",
                row["id"], row["document_id"], row["doc_tenant_id"], attempts, event_status,
                json.dumps({"stage_at_expiry": row["stage"], "attempts": attempts, "max_attempts": max_attempts}),
            )
            reaped += 1
    if reaped:
        log.warning("Reaped %d ingestion job(s) with expired leases", reaped)
    return reaped


async def run_loop(
    storage: StorageProvider,
    embedding_manager: EmbeddingProviderManager,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> None:
    """Worker entry point; runs forever, holding no transaction between polls."""
    pool = await db.get_pool()
    last_reap = 0.0
    while True:
        if time.monotonic() - last_reap >= REAP_INTERVAL_S:
            last_reap = time.monotonic()
            try:
                await reap_expired_leases(pool)
            except Exception:
                log.exception("lease reaper failed; will retry next interval")
        try:
            job = await _claim_next_job(pool)
            if job is None:
                await asyncio.sleep(poll_interval_s)
                continue
            await process_one_job(pool, storage, embedding_manager, job)
        except Exception:
            # process_one_job records its own failures; this is a DB-level fault. The lease reaper
            # recovers a job left running, so keep the worker alive.
            log.exception("ingestion loop iteration failed")
            await asyncio.sleep(poll_interval_s)
