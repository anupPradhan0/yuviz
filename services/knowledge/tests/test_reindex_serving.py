"""A document that is already indexed keeps serving its old chunks through every step of a re-ingest, including a
failing, retried, dead-lettered or lease-reaped one. `searchable` replicates the retrieval status filter of the
integrated retrieval code (ready or ready_degraded, not deleted, auto mode, embedded, not a parent)."""

from __future__ import annotations

import httpx
import pytest

from libs.tenancy import set_caller_tenant
from services.knowledge import documents as documents_service
from services.knowledge import embedding_manager as em
from services.knowledge.ingestion_worker import _claim_next_job, process_one_job, reap_expired_leases
from services.knowledge.reingest import requeue_documents
from services.knowledge.storage import LocalStorageProvider
from services.knowledge.tests.test_ingestion_queue import _drain_pending, _make_kb, _manager, _purge_tenant, _upload

_DOC = ("# Handbook\n\n## Returns\n\n" + "\n\n".join(
    f"Paragraph {i}: refunds are processed within thirty days of the original purchase date provided the item is "
    f"returned in its original packaging with proof of purchase and a completed return form number {i}. " * 3
    for i in range(10)) + "\n\n## Billing\n\nInvoices are emailed monthly.\n").encode()

_SEARCHABLE = (
    "SELECT count(*) FROM kb_chunks c JOIN kb_documents d ON d.id = c.document_id AND d.deleted_at IS NULL "
    "AND d.usage_mode = 'auto' AND d.status IN ('ready', 'ready_degraded') "
    "WHERE c.document_id = $1 AND c.embedding IS NOT NULL AND NOT c.is_parent"
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_CHUNKER", "structure")
    monkeypatch.setattr(em, "EMBED_RETRY_BASE_S", 0.0)
    monkeypatch.setattr(em, "EMBED_BATCH_RETRIES", 0)


class Provider:
    def __init__(self, pool, doc_id=None, fail=None):
        self.pool, self.doc_id, self.fail, self.seen = pool, doc_id, fail, []

    async def embed(self, texts):
        if self.doc_id is not None:
            self.seen.append((await self.pool.fetchval(_SEARCHABLE, self.doc_id),
                              await self.pool.fetchval("SELECT status FROM kb_documents WHERE id = $1", self.doc_id)))
        if self.fail is not None:
            raise self.fail
        return [[0.1] * 768 for _ in texts]


def _http(status):
    req = httpx.Request("POST", "https://user:secret@embed.example.com/v1?key=sk")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(status, request=req))


async def _ingest(pool, tenant, kb, provider, title="handbook"):
    doc = await _upload(tenant, kb, title, content=_DOC, content_type="text/markdown")
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    await process_one_job(pool, LocalStorageProvider(), _manager(provider), job)
    return doc


async def _reingest_once(pool, tenant, provider, *, max_attempts=None):
    set_caller_tenant(None)
    ids = await requeue_documents(pool, tenant["id"])
    assert len(ids) == 1
    if max_attempts:
        await pool.execute("UPDATE kb_ingestion_jobs SET max_attempts = $2 WHERE document_id = $1 AND status = 'pending'",
                           ids[0], max_attempts)
    job = await _claim_next_job(pool, tenant_cap=100)
    await process_one_job(pool, LocalStorageProvider(), _manager(provider), job)
    return ids[0], job


async def _state(pool, doc_id):
    row = dict(await pool.fetchrow("SELECT status, error, version FROM kb_documents WHERE id = $1", doc_id))
    return row, await pool.fetchval(_SEARCHABLE, doc_id)


async def test_a_successful_reingest_keeps_the_document_searchable_at_every_step(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "serve-ok-kb")
    doc = await _ingest(pool, tenant, kb, Provider(pool))
    row, n = await _state(pool, doc["id"])
    assert row["status"] == "ready" and n > 3

    probe = Provider(pool, doc["id"])
    set_caller_tenant(None)
    await requeue_documents(pool, tenant["id"])
    row, now = await _state(pool, doc["id"])
    assert row["status"] == "ready" and now == n                      # after the version bump
    set_caller_tenant(str(tenant["id"]))
    assert next(d for d in await documents_service.list_documents(kb["id"]) if d["id"] == doc["id"])["chunk_count"] == n
    job = await _claim_next_job(pool, tenant_cap=100)
    await process_one_job(pool, LocalStorageProvider(), _manager(probe), job)
    assert probe.seen and all(c == n and st == "ready" for c, st in probe.seen)  # while the worker is embedding
    row, after = await _state(pool, doc["id"])
    assert row["status"] == "ready" and row["error"] is None and after > 0
    set_caller_tenant(str(tenant["id"]))
    listed = next(d for d in await documents_service.list_documents(kb["id"]) if d["id"] == doc["id"])
    assert listed["chunk_count"] == after
    await _purge_tenant(pool, tenant["id"])


async def test_first_time_ingest_still_shows_processing(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "serve-first-kb")
    doc = await _upload(tenant, kb, "first", content=_DOC, content_type="text/markdown")
    probe = Provider(pool, doc["id"])
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    await process_one_job(pool, LocalStorageProvider(), _manager(probe), job)
    assert probe.seen[0] == (0, "processing")
    assert (await _state(pool, doc["id"]))[0]["status"] == "ready"
    await _purge_tenant(pool, tenant["id"])


@pytest.mark.parametrize("failure,klass", [(_http(401), "permanent"), (RuntimeError("provider down"), "transient")])
async def test_a_failing_reingest_keeps_serving_and_records_the_reason(tenant_agent, pool, failure, klass):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, f"serve-fail-{klass}-kb")
    doc = await _ingest(pool, tenant, kb, Provider(pool))
    _, n = await _state(pool, doc["id"])

    probe = Provider(pool, doc["id"], fail=failure)
    _, job = await _reingest_once(pool, tenant, probe)
    row, after = await _state(pool, doc["id"])
    assert row["status"] == "ready" and after == n and all(c == n for c, _ in probe.seen)
    assert row["error"].startswith("re-index failed: ") and "secret" not in row["error"] and "sk" not in row["error"].split("key")[-1]
    j = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", job["id"]))
    assert j["error_class"] == klass and j["status"] == ("failed" if klass == "permanent" else "pending")
    await _purge_tenant(pool, tenant["id"])


async def test_a_dead_lettered_reingest_keeps_serving(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "serve-dlq-kb")
    doc = await _ingest(pool, tenant, kb, Provider(pool))
    _, n = await _state(pool, doc["id"])
    _, job = await _reingest_once(pool, tenant, Provider(pool, fail=RuntimeError("down")), max_attempts=1)
    assert (await pool.fetchval("SELECT status FROM kb_ingestion_jobs WHERE id = $1", job["id"])) == "dead_letter"
    row, after = await _state(pool, doc["id"])
    assert row["status"] == "ready" and after == n and row["error"].startswith("re-index failed: ")
    await _purge_tenant(pool, tenant["id"])


@pytest.mark.parametrize("exhausted", [False, True])
async def test_a_lease_reap_of_a_reingest_keeps_serving(tenant_agent, pool, exhausted):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, f"serve-reap-{exhausted}-kb")
    doc = await _ingest(pool, tenant, kb, Provider(pool))
    _, n = await _state(pool, doc["id"])
    set_caller_tenant(None)
    await requeue_documents(pool, tenant["id"])
    job = await _claim_next_job(pool, tenant_cap=100)           # a worker claims it, then "crashes"
    await pool.execute("UPDATE kb_ingestion_jobs SET lease_expires_at = now() - interval '1 second', max_attempts = $2 WHERE id = $1",
                       job["id"], 1 if exhausted else 5)
    assert await reap_expired_leases(pool) >= 1
    row, after = await _state(pool, doc["id"])
    assert row["status"] == "ready" and after == n and row["error"].startswith("re-index failed: ")
    assert (await pool.fetchval("SELECT status FROM kb_ingestion_jobs WHERE id = $1", job["id"])) == ("dead_letter" if exhausted else "pending")
    await _purge_tenant(pool, tenant["id"])


async def test_first_time_failures_and_reaps_still_take_the_document_through_the_old_states(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "serve-first-fail-kb")
    doc = await _upload(tenant, kb, "never-ready", content=_DOC, content_type="text/markdown")
    job = await _claim_next_job(pool, tenant_cap=100)
    await pool.execute("UPDATE kb_ingestion_jobs SET lease_expires_at = now() - interval '1 second', max_attempts = 1 WHERE id = $1", job["id"])
    await reap_expired_leases(pool)
    assert (await pool.fetchval("SELECT status FROM kb_documents WHERE id = $1", doc["id"])) == "failed"
    set_caller_tenant(str(tenant["id"]))
    retried = await documents_service.retry_document(doc["id"], tenant_id=tenant["id"])  # admin retry of a failed first ingest
    assert retried is not None and retried["status"] == "pending" and retried["error"] is None
    await _purge_tenant(pool, tenant["id"])


async def test_admin_can_retry_a_failed_reingest_of_a_ready_document(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "serve-retry-kb")
    doc = await _ingest(pool, tenant, kb, Provider(pool))
    await _reingest_once(pool, tenant, Provider(pool, fail=_http(401)))
    set_caller_tenant(str(tenant["id"]))
    retried = await documents_service.retry_document(doc["id"], tenant_id=tenant["id"])
    assert retried is not None and retried["status"] == "ready"
    assert await documents_service.retry_document(doc["id"], tenant_id=tenant["id"]) is None   # now has an active job
    job = await _claim_next_job(pool, tenant_cap=100)
    await process_one_job(pool, LocalStorageProvider(), _manager(Provider(pool)), job)
    row, n = await _state(pool, doc["id"])
    assert row["status"] == "ready" and row["error"] is None and n > 0
    # a healthy ready document cannot be "retried"
    assert await documents_service.retry_document(doc["id"], tenant_id=tenant["id"]) is None
    await _purge_tenant(pool, tenant["id"])


async def test_the_api_says_when_retry_will_work(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "retryable-kb")
    doc = await _ingest(pool, tenant, kb, Provider(pool))

    async def retryable():
        set_caller_tenant(str(tenant["id"]))
        listed = next(d for d in await documents_service.list_documents(kb["id"]) if d["id"] == doc["id"])
        assert (await documents_service.get_document(doc["id"]))["retryable"] == listed["retryable"]
        return listed["retryable"]

    assert await retryable() is False                                   # healthy ready document
    await _reingest_once(pool, tenant, Provider(pool, fail=_http(401)))
    assert await retryable() is True                                    # ready, last re-index failed, no active job
    await _reingest_once(pool, tenant, Provider(pool, fail=RuntimeError("down")))   # automatic retry now pending
    assert (await pool.fetchval("SELECT count(*) FROM kb_ingestion_jobs WHERE document_id = $1 AND status = 'pending'", doc["id"])) == 1
    assert await retryable() is False                                   # clicking Retry would 409
    await pool.execute("UPDATE kb_ingestion_jobs SET status = 'failed' WHERE document_id = $1 AND status = 'pending'", doc["id"])
    await pool.execute("UPDATE kb_documents SET status = 'failed' WHERE id = $1", doc["id"])
    assert await retryable() is True                                    # a failed first ingest
    await _purge_tenant(pool, tenant["id"])
