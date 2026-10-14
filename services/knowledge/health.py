"""Tenant-scoped ingestion and retrieval health for the admin UI. Every query runs on tenant_conn, so RLS is what
keeps another tenant's jobs and logs out of the numbers, and there is deliberately no platform_conn here.
Each query also names the tenant (app.tenant_id, set by tenant_conn): RLS does not apply to a superuser or
table-owner connection (dev and CI databases), and a health page must not depend on the role the pool runs as.
Platform-scoped callers get the tenant named in the path, exactly like the other tenant routes."""

from __future__ import annotations

import json
from typing import Any

from libs.tenancy import tenant_conn

from . import db
from .ingestion_worker import _URL_RE, _scrub_url

WINDOW_HOURS = 24
FAILED_DOCUMENT_LIMIT = 50
EVENT_LIMIT = 200

_QUEUE_SQL = """
SELECT count(*) FILTER (WHERE j.status = 'pending')                                   AS pending,
       count(*) FILTER (WHERE j.status = 'running')                                   AS running,
       count(*) FILTER (WHERE j.status = 'running' AND j.lease_expires_at < now())    AS stuck_running,
       count(*) FILTER (WHERE j.status = 'dead_letter')                               AS dead_letter,
       extract(epoch FROM now() - min(j.created_at) FILTER (WHERE j.status = 'pending')) AS oldest_pending_age_s
  FROM kb_ingestion_jobs j
 WHERE EXISTS (SELECT 1 FROM kb_documents d
                WHERE d.id = j.document_id AND d.deleted_at IS NULL
                  AND d.tenant_id = current_setting('app.tenant_id')::uuid)
"""

# One row per document that is currently failed; the document's own error column is the reason.
_FAILED_SQL = """
SELECT d.id, d.kb_id, d.title, d.error, d.updated_at,
       j.status AS job_status, j.error_class, j.attempts
  FROM kb_documents d
  LEFT JOIN LATERAL (SELECT status, error_class, attempts FROM kb_ingestion_jobs
                      WHERE document_id = d.id ORDER BY created_at DESC LIMIT 1) j ON true
 WHERE d.status = 'failed' AND d.deleted_at IS NULL AND d.tenant_id = current_setting('app.tenant_id')::uuid
 ORDER BY d.updated_at DESC
 LIMIT $1
"""

_RETRIEVAL_SQL = """
SELECT count(*)                                                      AS total,
       count(*) FILTER (WHERE status = 'ok')                         AS ok,
       count(*) FILTER (WHERE status = 'no_match')                   AS no_match,
       count(*) FILTER (WHERE status = 'partial')                    AS partial,
       count(*) FILTER (WHERE status = 'unavailable')                AS unavailable,
       count(*) FILTER (WHERE status = 'error')                      AS error,
       count(*) FILTER (WHERE cardinality(degraded) > 0)             AS degraded,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY (timings_ms->>'total')::float8) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY (timings_ms->>'total')::float8) AS p95,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY (timings_ms->>'total')::float8) AS p99
  FROM retrieval_logs
 WHERE tenant_id = current_setting('app.tenant_id')::uuid AND created_at >= now() - make_interval(hours => $1)
"""

_DEGRADED_SQL = """
SELECT d AS reason, count(*) AS n
  FROM retrieval_logs, unnest(degraded) AS d
 WHERE tenant_id = current_setting('app.tenant_id')::uuid AND created_at >= now() - make_interval(hours => $1)
 GROUP BY d ORDER BY n DESC LIMIT 20
"""


def scrub(text: str | None) -> str | None:
    """Stored errors are redacted at write time; scrubbing again on read covers rows written before that."""
    return None if text is None else _URL_RE.sub(_scrub_url, text)[:1000]


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


async def tenant_health() -> dict[str, Any]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        queue = dict(await conn.fetchrow(_QUEUE_SQL))
        failed = await conn.fetch(_FAILED_SQL, FAILED_DOCUMENT_LIMIT)
        stats = dict(await conn.fetchrow(_RETRIEVAL_SQL, WINDOW_HOURS))
        degraded = await conn.fetch(_DEGRADED_SQL, WINDOW_HOURS)
    total = stats["total"]
    return {
        "queue": {
            "pending": queue["pending"], "running": queue["running"], "stuck_running": queue["stuck_running"],
            "dead_letter": queue["dead_letter"], "oldest_pending_age_s": _round(queue["oldest_pending_age_s"]),
        },
        "failed_documents": [
            {"id": str(r["id"]), "kb_id": str(r["kb_id"]), "title": r["title"], "error": scrub(r["error"]),
             "job_status": r["job_status"], "error_class": r["error_class"], "attempts": r["attempts"],
             "updated_at": r["updated_at"].isoformat()}
            for r in failed
        ],
        "retrieval_24h": {
            "window_hours": WINDOW_HOURS,
            "total": total,
            "by_status": {k: stats[k] for k in ("ok", "no_match", "partial", "unavailable", "error")},
            "no_match_rate": round(stats["no_match"] / total, 4) if total else None,
            "degraded": stats["degraded"],
            "degraded_by_reason": {r["reason"]: r["n"] for r in degraded},
            "latency_ms": {"p50": _round(stats["p50"]), "p95": _round(stats["p95"]), "p99": _round(stats["p99"])},
        },
    }


async def document_events(document_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        # Newest first, bounded; re-order oldest-first for reading.
        rows = await conn.fetch(
            "SELECT job_id, attempt, stage, status, duration_ms, detail, created_at FROM kb_ingestion_events "
            "WHERE document_id = $1 AND tenant_id = current_setting('app.tenant_id')::uuid "
            "ORDER BY created_at DESC, id DESC LIMIT $2", document_id, EVENT_LIMIT,
        )
    out = []
    for r in reversed(rows):
        detail = json.loads(r["detail"]) if isinstance(r["detail"], str) else dict(r["detail"])
        if isinstance(detail.get("error"), str):
            detail["error"] = scrub(detail["error"])
        out.append({
            "job_id": str(r["job_id"]), "attempt": r["attempt"], "stage": r["stage"], "status": r["status"],
            "duration_ms": r["duration_ms"], "detail": detail, "created_at": r["created_at"].isoformat(),
        })
    return out
