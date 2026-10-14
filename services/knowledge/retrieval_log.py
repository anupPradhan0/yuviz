"""retrieval_logs writer: one row per retrieval, off the response path.
record() is a put_nowait onto a bounded queue; one background task drains it. A slow or broken database can
therefore cost a dropped log line (counted, warned about) but never latency, and never unbounded memory. Each
tenant also has a token bucket (default 5 rows/s, burst 20: at most 432,000 rows per tenant per day, about
13M over the 30-day retention), so one noisy tenant cannot fill the queue or the table; over-limit rows are
dropped and counted.

What is stored: the caller's words are speech. Before storing, the query is cut to 500 characters, runs of six
or more digits (spaces/dashes between digits allowed: phone and card numbers) become "[digits]" and e-mail
addresses become "[email]"; runs of six or more spoken digit words ("four one five five five ...") too. Digit runs
are caught across single or double spaces, dashes, dots, commas, slashes, underscores and " - ", in any Unicode
digit script (fullwidth, Devanagari, ...). NOT redacted: names, addresses, free-text account details, numbers
shorter than six digits, digits split by words ("my pin is 12 oh 34 56"), spoken digits in other languages,
deliberately obfuscated numbers. Prices and dates with six or more digits are masked too (false positives). Rows carry a
trace_id but no call/session id, so they are not linked to a call; they are tenant-scoped by RLS and purged after
KNOWLEDGE_RETRIEVAL_LOG_RETENTION_DAYS (default 30)."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import db

log = logging.getLogger(__name__)

QUERY_MAX_CHARS = 500
# Six or more digits (any script: \d is Unicode) with up to three separator characters between neighbours
# (space, dash, dot, comma, slash, underscore, en/em dash): phone and card numbers however an STT engine
# punctuates them. Also six or more spoken digit words in a row.
_SEP = r"[\s.,/_\-\u2013\u2014]"
_DIGIT_RUN_RE = re.compile(rf"\d(?:{_SEP}{{0,3}}\d){{5,}}")
_DIGIT_WORDS = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)"
_SPOKEN_DIGITS_RE = re.compile(rf"\b{_DIGIT_WORDS}(?:{_SEP}{{1,3}}{_DIGIT_WORDS}\b){{5,}}", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_RATE_PER_S = float(os.environ.get("KNOWLEDGE_RETRIEVAL_LOG_RATE_PER_S", "5"))
_BURST = float(os.environ.get("KNOWLEDGE_RETRIEVAL_LOG_BURST", "20"))
_RETENTION_DAYS = int(os.environ.get("KNOWLEDGE_RETRIEVAL_LOG_RETENTION_DAYS", "30"))
_PURGE_BATCH = 1000
_PURGE_INTERVAL_S = 3600.0
_QUEUE_MAX = int(os.environ.get("KNOWLEDGE_RETRIEVAL_LOG_QUEUE", "500"))


@dataclass(frozen=True)
class RetrievalLogRow:
    tenant_id: str
    agent_id: str | None
    trace_id: str
    query: str
    status: str
    degraded: list[str]
    chunk_ids: list[str]
    top_score: float | None
    candidates: int
    timings_ms: dict[str, float]


_queue: asyncio.Queue[RetrievalLogRow] | None = None
_worker: asyncio.Task | None = None
_loop: asyncio.AbstractEventLoop | None = None
dropped = 0
dropped_rate_limited = 0
_buckets: dict[str, tuple[float, float]] = {}  # tenant_id -> (tokens, last refill)


def redact_query(query: str) -> str:
    """Cap first (so the regexes only ever see a bounded string), then mask digits and e-mail addresses."""
    capped = query[: QUERY_MAX_CHARS * 2]
    masked = _EMAIL_RE.sub("[email]", _DIGIT_RUN_RE.sub("[digits]", _SPOKEN_DIGITS_RE.sub("[digits]", capped)))
    return masked[:QUERY_MAX_CHARS]


def _allow(tenant_id: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    tokens, last = _buckets.get(tenant_id, (_BURST, now))
    tokens = min(_BURST, tokens + (now - last) * _RATE_PER_S)
    if tokens < 1.0:
        _buckets[tenant_id] = (tokens, now)
        return False
    _buckets[tenant_id] = (tokens - 1.0, now)
    if len(_buckets) > 10000:  # bounded memory: forget the stalest half
        for key in sorted(_buckets, key=lambda k: _buckets[k][1])[:5000]:
            del _buckets[key]
    return True


async def _insert(row: RetrievalLogRow) -> None:
    pool = await db.get_pool()
    # Explicit tenant: this task outlives the request, so it must not lean on the request's scope.
    async with tenant_conn(pool, explicit_tenant=row.tenant_id, reason="retrieval-log") as conn:
        await conn.execute(
            "INSERT INTO retrieval_logs (tenant_id, agent_id, trace_id, query, status, degraded, chunk_ids, "
            "top_score, candidates, timings_ms) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::uuid[], $8, $9, $10::jsonb)",
            row.tenant_id, row.agent_id, row.trace_id, redact_query(row.query), row.status, row.degraded,
            row.chunk_ids, row.top_score, row.candidates, json.dumps(row.timings_ms),
        )


async def _drain(queue: asyncio.Queue[RetrievalLogRow]) -> None:
    while True:
        row = await queue.get()
        try:
            await asyncio.wait_for(_insert(row), timeout=5.0)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - logging must never take retrieval down
            log.warning("retrieval_logs insert failed trace=%s", row.trace_id, exc_info=True)
        finally:
            queue.task_done()


def _ensure_worker() -> asyncio.Queue[RetrievalLogRow]:
    global _queue, _worker, _loop
    loop = asyncio.get_running_loop()
    if _queue is None or _loop is not loop or _worker is None or _worker.done():
        _queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        _loop = loop
        # Empty context: the worker must not inherit the first request's tenant scope (it would then collide
        # with every later tenant's explicit_tenant). Each row names its own tenant.
        _worker = loop.create_task(_drain(_queue), context=contextvars.Context())
    return _queue


def record(row: RetrievalLogRow) -> None:
    """Fire and forget; never raises."""
    global dropped, dropped_rate_limited
    try:
        if not _allow(row.tenant_id):
            dropped_rate_limited += 1
            if dropped_rate_limited == 1 or dropped_rate_limited % 1000 == 0:
                log.warning("retrieval_logs per-tenant rate limit: %d row(s) dropped so far", dropped_rate_limited)
            return
        _ensure_worker().put_nowait(row)
    except asyncio.QueueFull:
        dropped += 1
        if dropped == 1 or dropped % 100 == 0:
            log.warning("retrieval_logs queue full, dropped %d row(s) so far", dropped)
    except Exception:  # noqa: BLE001
        log.warning("retrieval_logs enqueue failed", exc_info=True)


async def flush(timeout: float = 5.0) -> None:
    """Wait until everything queued so far is written (tests, graceful shutdown)."""
    if _queue is not None:
        await asyncio.wait_for(_queue.join(), timeout=timeout)


async def shutdown(timeout: float = 5.0) -> None:
    """Drain what is queued (bounded wait), then stop the worker. Call before db.close_pool(): a pool must
    never be used, or reopened, after it is closed."""
    global _queue, _worker
    worker = _worker
    try:
        await flush(timeout)
    except Exception:  # noqa: BLE001 - shutting down; whatever did not drain is dropped
        pass
    if worker is not None and not worker.done():
        worker.cancel()
        try:
            await worker
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _worker = None
    _queue = None


async def purge_once(retention_days: int | None = None) -> int:
    """Delete rows older than the retention window in small batches (no long lock). Returns rows deleted.
    Cross-tenant by nature, hence the one platform_conn reason; it only ever deletes by age."""
    days = _RETENTION_DAYS if retention_days is None else retention_days
    pool = await db.get_pool()
    total = 0
    while True:
        async with platform_conn(pool, reason="retrieval-logs-purge") as conn:
            deleted = await conn.fetchval(
                "WITH doomed AS (SELECT id FROM retrieval_logs WHERE created_at < now() - make_interval(days => $1) "
                "LIMIT $2 FOR UPDATE SKIP LOCKED), gone AS (DELETE FROM retrieval_logs WHERE id IN "
                "(SELECT id FROM doomed) RETURNING 1) SELECT count(*) FROM gone", days, _PURGE_BATCH,
            )
        total += deleted
        if deleted < _PURGE_BATCH:
            return total
        await asyncio.sleep(0.05)


async def purge_loop() -> None:
    """Lifespan task: purge hourly. Failures are logged and retried next tick."""
    while True:
        try:
            n = await purge_once()
            if n:
                log.info("retrieval_logs purge deleted %d row(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("retrieval_logs purge failed", exc_info=True)
        await asyncio.sleep(_PURGE_INTERVAL_S)
