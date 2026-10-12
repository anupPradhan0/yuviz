"""retrieval_logs writer: one row per retrieval, off the response path.
record() is a put_nowait onto a bounded queue; one background task drains it. A slow or broken database can
therefore cost a dropped log line (counted, warned about) but never latency, and never unbounded memory."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from libs.tenancy import tenant_conn

from . import db

log = logging.getLogger(__name__)

QUERY_MAX_CHARS = 1000
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


async def _insert(row: RetrievalLogRow) -> None:
    pool = await db.get_pool()
    # Explicit tenant: this task outlives the request, so it must not lean on the request's scope.
    async with tenant_conn(pool, explicit_tenant=row.tenant_id, reason="retrieval-log") as conn:
        await conn.execute(
            "INSERT INTO retrieval_logs (tenant_id, agent_id, trace_id, query, status, degraded, chunk_ids, "
            "top_score, candidates, timings_ms) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::uuid[], $8, $9, $10::jsonb)",
            row.tenant_id, row.agent_id, row.trace_id, row.query[:QUERY_MAX_CHARS], row.status, row.degraded,
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
    global dropped
    try:
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
