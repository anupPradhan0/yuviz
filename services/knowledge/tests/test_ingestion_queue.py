"""Ingestion queue hardening: SKIP LOCKED claim, lease/heartbeat/reaper, retry + dead-letter, fencing, events.
Uses a fake embedding provider, so these need Postgres but not Ollama."""

from __future__ import annotations

import asyncio

from libs.tenancy import set_caller_tenant
from services.config import provider_configs
from services.knowledge import documents as documents_service
from services.knowledge import embedding_manager
from services.knowledge import ingestion_worker as worker
from services.knowledge import knowledge_bases as kb_service
from services.knowledge.embedding_manager import EmbeddingProviderManager
from services.knowledge.ingestion_worker import (
    _claim_next_job, _heartbeat, backoff_seconds, classify_error, process_one_job, reap_expired_leases,
    redact_error,
)
from services.knowledge.chunking import count_tokens
from services.knowledge.secret_resolver import CompositeSecretResolver
from services.knowledge.storage import LocalStorageProvider

_BIG = (b"Refunds are processed within 30 days of the original purchase date, provided the item "
        b"is returned in its original packaging with proof of purchase. Shipping takes 3 to 5 "
        b"business days for domestic orders and 7 to 14 for international orders. ") * 3


class _FakeProvider:
    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("embedding provider timeout")
        return [[0.1] * 768 for _ in texts]


def _manager(provider: _FakeProvider) -> EmbeddingProviderManager:
    async def factory(cfg, api_key):
        return provider
    return EmbeddingProviderManager(CompositeSecretResolver(), registry={"ollama": factory})


async def _make_kb(tenant, slug: str):
    set_caller_tenant(str(tenant["id"]))
    cfg = await provider_configs.create_provider_config(
        tenant_id=tenant["id"], name=f"Embed-{slug}", role="embedding", engine="ollama", allow_pointer_schemes=False,
    )
    return await kb_service.create_knowledge_base(
        tenant_id=tenant["id"], slug=slug, name=slug, embedding_config_id=cfg["id"],
    )


async def _upload(
    tenant, kb, title: str, content: bytes = _BIG, content_type: str = "text/plain", language: str | None = None,
):
    set_caller_tenant(str(tenant["id"]))
    return await documents_service.upload_document(
        tenant_id=tenant["id"], kb_id=kb["id"], title=title, filename=f"{title}.txt",
        content_type=content_type, content=content, storage=LocalStorageProvider(), language=language,
    )


async def _purge_tenant(pool, tenant_id):
    """For tests that create data in a tenant whose fixture does not clean up knowledge rows."""
    await pool.execute("DELETE FROM kb_chunks WHERE tenant_id = $1", tenant_id)
    await pool.execute("DELETE FROM kb_ingestion_events WHERE tenant_id = $1", tenant_id)
    await pool.execute(
        "DELETE FROM kb_ingestion_jobs WHERE document_id IN (SELECT id FROM kb_documents WHERE tenant_id = $1)",
        tenant_id,
    )
    await pool.execute("DELETE FROM audit_log WHERE tenant_id = $1", tenant_id)
    await pool.execute("DELETE FROM kb_documents WHERE tenant_id = $1", tenant_id)
    await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", tenant_id)
    await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tenant_id)


async def _job(pool, job_id):
    return dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", job_id))


async def _events(pool, job_id):
    rows = await pool.fetch("SELECT * FROM kb_ingestion_events WHERE job_id = $1 ORDER BY created_at, id", job_id)
    return [dict(r) for r in rows]


async def _drain_pending(pool):
    """Park jobs left by other tests so claim assertions only see this test's rows."""
    await pool.execute(
        "UPDATE kb_ingestion_jobs SET next_attempt_at = now() + interval '1 day' WHERE status = 'pending'",
    )


# ── pure helpers ────────────────────────────────────────────────────────

def test_classify_error():
    assert classify_error(ValueError("unsupported content_type")) == "permanent"
    assert classify_error(FileNotFoundError("gone")) == "permanent"
    assert classify_error(RuntimeError("timeout")) == "transient"
    assert classify_error(ConnectionError("reset")) == "transient"


def test_backoff_is_exponential_capped_and_jittered():
    assert backoff_seconds(1, rand=0.5) == worker.BACKOFF_BASE_S
    assert backoff_seconds(2, rand=0.5) == worker.BACKOFF_BASE_S * 2
    assert backoff_seconds(30, rand=0.5) == worker.BACKOFF_CAP_S
    assert backoff_seconds(3, rand=0.0) < backoff_seconds(3, rand=1.0)
    assert 0.8 * worker.BACKOFF_CAP_S <= backoff_seconds(30, rand=0.0) <= 1.2 * worker.BACKOFF_CAP_S


# ── claim ───────────────────────────────────────────────────────────────

async def test_concurrent_claims_never_return_the_same_job(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "claim-kb")
    ids = {(await _upload(tenant, kb, f"d{i}"))["ingestion_job_id"] for i in range(6)}

    claimed = await asyncio.gather(*[_claim_next_job(pool, tenant_cap=100) for _ in range(10)])

    got = [c["id"] for c in claimed if c is not None]
    assert len(got) == len(set(got)) == 6
    assert set(got) == ids
    assert all(c["status"] == "running" and c["attempts"] == 1 and c["lease_expires_at"] for c in claimed if c)


async def test_claim_respects_the_per_tenant_running_cap(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "cap-kb")
    for i in range(3):
        await _upload(tenant, kb, f"c{i}")

    first = await _claim_next_job(pool, tenant_cap=1)
    second = await _claim_next_job(pool, tenant_cap=1)
    assert first is not None
    assert second is None  # this tenant already has one running; the cap holds the rest back


async def test_claim_skips_jobs_not_yet_due(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "due-kb")
    doc = await _upload(tenant, kb, "later")
    await pool.execute(
        "UPDATE kb_ingestion_jobs SET next_attempt_at = now() + interval '10 minutes' WHERE id = $1",
        doc["ingestion_job_id"],
    )
    assert await _claim_next_job(pool, tenant_cap=100) is None


# ── retry, dead-letter, permanent ───────────────────────────────────────

async def test_transient_failure_retries_with_backoff_then_succeeds(tenant_agent, pool, monkeypatch):
    monkeypatch.setattr(embedding_manager, "EMBED_BATCH_RETRIES", 0)  # isolate the job-level retry from the in-batch one
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "retry-kb")
    doc = await _upload(tenant, kb, "flaky")
    provider = _FakeProvider(fail_times=1)
    manager = _manager(provider)

    job = await _claim_next_job(pool, tenant_cap=100)
    await process_one_job(pool, LocalStorageProvider(), manager, job)

    after_fail = await _job(pool, doc["ingestion_job_id"])
    assert after_fail["status"] == "pending"
    assert after_fail["error_class"] == "transient"
    assert after_fail["attempts"] == 1
    assert after_fail["next_attempt_at"] > after_fail["updated_at"]
    assert after_fail["lease_expires_at"] is None
    assert (await documents_service.get_document(doc["id"]))["status"] == "pending"
    assert await _claim_next_job(pool, tenant_cap=100) is None  # still backing off

    await pool.execute("UPDATE kb_ingestion_jobs SET next_attempt_at = now() WHERE id = $1", doc["ingestion_job_id"])
    job = await _claim_next_job(pool, tenant_cap=100)
    assert job["attempts"] == 2
    await process_one_job(pool, LocalStorageProvider(), manager, job)

    final = await _job(pool, doc["ingestion_job_id"])
    assert final["status"] == "succeeded" and final["error"] is None
    updated = await documents_service.get_document(doc["id"])
    assert updated["status"] == "ready" and updated["error"] is None
    assert await pool.fetchval("SELECT count(*) FROM kb_chunks WHERE document_id = $1", doc["id"]) >= 1

    trail = [(e["attempt"], e["stage"], e["status"]) for e in await _events(pool, doc["ingestion_job_id"])]
    assert (1, "embed", "error") in trail and (1, "worker", "retry_scheduled") in trail
    assert (2, "embed", "ok") in trail and (2, "index", "ok") in trail


async def test_transient_failures_dead_letter_after_max_attempts(tenant_agent, pool, monkeypatch):
    monkeypatch.setattr(embedding_manager, "EMBED_BATCH_RETRIES", 0)
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "dlq-kb")
    doc = await _upload(tenant, kb, "doomed")
    await pool.execute("UPDATE kb_ingestion_jobs SET max_attempts = 2 WHERE id = $1", doc["ingestion_job_id"])
    manager = _manager(_FakeProvider(fail_times=99))

    for _ in range(2):
        await pool.execute("UPDATE kb_ingestion_jobs SET next_attempt_at = now() WHERE id = $1", doc["ingestion_job_id"])
        job = await _claim_next_job(pool, tenant_cap=100)
        await process_one_job(pool, LocalStorageProvider(), manager, job)

    final = await _job(pool, doc["ingestion_job_id"])
    assert final["status"] == "dead_letter" and final["attempts"] == 2
    assert "timeout" in final["error"] and final["stage"] == "embed"
    assert final["finished_at"] is not None
    updated = await documents_service.get_document(doc["id"])
    assert updated["status"] == "failed" and "timeout" in updated["error"]
    assert (await _events(pool, doc["ingestion_job_id"]))[-1]["status"] == "dead_letter"

    # An admin retry re-queues a dead-lettered document through the existing path.
    set_caller_tenant(str(tenant["id"]))
    retried = await documents_service.retry_document(doc["id"], tenant_id=tenant["id"])
    assert retried is not None and retried["status"] == "pending"


async def test_permanent_error_fails_immediately_without_retry(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "perm-kb")
    doc = await _upload(tenant, kb, "scan", content=b"%PDF-1.4 fake", content_type="application/pdf")

    job = await _claim_next_job(pool, tenant_cap=100)
    await process_one_job(pool, LocalStorageProvider(), _manager(_FakeProvider()), job)

    final = await _job(pool, doc["ingestion_job_id"])
    assert final["status"] == "failed" and final["error_class"] == "permanent" and final["attempts"] == 1
    assert final["stage"] == "extract"
    assert "no text" in (await documents_service.get_document(doc["id"]))["error"]  # a PDF with no readable content


# ── lease, heartbeat, reaper, fencing ───────────────────────────────────

async def test_reaper_requeues_a_job_whose_lease_expired(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "reap-kb")
    doc = await _upload(tenant, kb, "orphan")
    job = await _claim_next_job(pool, tenant_cap=100)  # a worker claims it, then "crashes"
    await pool.execute(
        "UPDATE kb_ingestion_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = $1", job["id"],
    )

    assert await reap_expired_leases(pool) >= 1

    after = await _job(pool, doc["ingestion_job_id"])
    assert after["status"] == "pending" and after["lease_expires_at"] is None
    assert "lease expired" in after["error"]
    assert (await documents_service.get_document(doc["id"]))["status"] == "pending"
    assert (await _events(pool, doc["ingestion_job_id"]))[-1]["status"] == "lease_expired"


async def test_reaper_dead_letters_when_attempts_are_exhausted(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "reap-dlq-kb")
    doc = await _upload(tenant, kb, "orphan2")
    await pool.execute("UPDATE kb_ingestion_jobs SET max_attempts = 1 WHERE id = $1", doc["ingestion_job_id"])
    job = await _claim_next_job(pool, tenant_cap=100)
    await pool.execute(
        "UPDATE kb_ingestion_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = $1", job["id"],
    )

    await reap_expired_leases(pool)

    assert (await _job(pool, doc["ingestion_job_id"]))["status"] == "dead_letter"
    assert (await documents_service.get_document(doc["id"]))["status"] == "failed"


async def test_reaper_leaves_a_live_lease_alone(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "live-kb")
    doc = await _upload(tenant, kb, "alive")
    await _claim_next_job(pool, tenant_cap=100)

    await reap_expired_leases(pool)

    assert (await _job(pool, doc["ingestion_job_id"]))["status"] == "running"


async def test_heartbeat_extends_the_lease(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "hb-kb")
    doc = await _upload(tenant, kb, "beat")
    job = await _claim_next_job(pool, lease_s=2.0, tenant_cap=100)
    before = job["lease_expires_at"]

    task = asyncio.create_task(_heartbeat(pool, job["id"], str(tenant["id"]), job["attempts"], 60.0, 0.05))
    await asyncio.sleep(0.4)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert (await _job(pool, doc["ingestion_job_id"]))["lease_expires_at"] > before


async def test_stale_worker_cannot_overwrite_a_job_another_worker_owns(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "fence-kb")
    doc = await _upload(tenant, kb, "contested")
    stale = await _claim_next_job(pool, tenant_cap=100)  # worker A, attempt 1
    # A stalls past its lease; the reaper requeues and worker B reclaims it as attempt 2.
    await pool.execute(
        "UPDATE kb_ingestion_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = $1", stale["id"],
    )
    await reap_expired_leases(pool)
    await pool.execute("UPDATE kb_ingestion_jobs SET next_attempt_at = now() WHERE id = $1", stale["id"])
    current = await _claim_next_job(pool, tenant_cap=100)
    assert current["attempts"] == 2

    await process_one_job(pool, LocalStorageProvider(), _manager(_FakeProvider()), stale)  # A finally finishes

    job = await _job(pool, doc["ingestion_job_id"])
    assert job["status"] == "running" and job["attempts"] == 2  # B's claim is untouched
    assert await pool.fetchval("SELECT count(*) FROM kb_chunks WHERE document_id = $1", doc["id"]) == 0


# ── events and schema ───────────────────────────────────────────────────

async def test_success_records_a_stage_trail_with_the_tenant(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "trail-kb")
    doc = await _upload(tenant, kb, "trail")

    job = await _claim_next_job(pool, tenant_cap=100)
    await process_one_job(pool, LocalStorageProvider(), _manager(_FakeProvider()), job)

    events = await _events(pool, doc["ingestion_job_id"])
    assert [(e["stage"], e["status"]) for e in events] == [
        ("read", "ok"), ("extract", "ok"), ("chunk", "ok"), ("embed", "ok"), ("index", "ok"),
    ]
    assert all(e["tenant_id"] == tenant["id"] and e["duration_ms"] is not None for e in events)


async def test_tiny_document_skips_embedding_and_still_succeeds(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "tiny-q-kb")
    doc = await _upload(tenant, kb, "tiny", content=b"We are closed on Sundays.")
    provider = _FakeProvider()

    job = await _claim_next_job(pool, tenant_cap=100)
    await process_one_job(pool, LocalStorageProvider(), _manager(provider), job)

    assert provider.calls == 0
    assert (await documents_service.get_document(doc["id"]))["usage_mode"] == "prompt"
    assert (await _job(pool, doc["ingestion_job_id"]))["status"] == "succeeded"


async def test_events_table_has_a_tenant_isolation_policy(pool):
    row = await pool.fetchrow(
        "SELECT p.polname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
        "LEFT JOIN pg_policy p ON p.polrelid = c.oid WHERE c.relname = 'kb_ingestion_events'",
    )
    assert row["polname"] == "kb_ingestion_events_tenant_isolation"
    assert row["relrowsecurity"] and row["relforcerowsecurity"]


# ── review-driven regressions ───────────────────────────────────────────

def test_redact_error_strips_url_credentials_and_query():
    exc = RuntimeError("Client error '401' for url 'https://admin:s3cret@ollama.internal:11434/api/embed?key=abc123#frag'")
    out = redact_error(exc)
    assert "s3cret" not in out and "admin" not in out and "abc123" not in out
    assert out.startswith("RuntimeError: ") and "ollama.internal:11434/api/embed" in out
    assert redact_error(ValueError("unsupported content_type='application/pdf'")) == (
        "ValueError: unsupported content_type='application/pdf'"
    )


async def test_stored_errors_never_contain_url_credentials(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "redact-kb")
    doc = await _upload(tenant, kb, "leaky")

    class _Leaky:
        async def embed(self, texts):
            raise RuntimeError("500 for url 'https://u:hunter2@host.example/embed?api_key=zzz'")

    job = await _claim_next_job(pool, tenant_cap=100)
    await process_one_job(pool, LocalStorageProvider(), _manager(_Leaky()), job)

    stored = [(await _job(pool, doc["ingestion_job_id"]))["error"],
              (await documents_service.get_document(doc["id"]))["error"]]
    stored += [str(e["detail"]) for e in await _events(pool, doc["ingestion_job_id"])]
    assert all("hunter2" not in t and "zzz" not in t for t in stored)


async def test_prompt_mode_document_is_sized_by_language_aware_token_count(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "lang-kb")
    text = "退款将在三十天内处理完毕。" * 2
    doc = await _upload(tenant, kb, "zh", content=text.encode(), language="zh")
    assert len(text.encode()) < worker.AUTO_INLINE_THRESHOLD_BYTES

    job = await _claim_next_job(pool, tenant_cap=100)
    await process_one_job(pool, LocalStorageProvider(), _manager(_FakeProvider()), job)

    token_count = await pool.fetchval("SELECT token_count FROM kb_chunks WHERE document_id = $1", doc["id"])
    assert token_count == count_tokens(text, "zh") and token_count > 1  # word-splitting would give 1


async def test_unspaced_language_document_is_chunked_by_characters(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "th-kb")
    text = "สวัสดีครับยินดีต้อนรับสู่ร้านของเรา" * 80  # no spaces, far over the inline threshold
    doc = await _upload(tenant, kb, "th", content=text.encode(), language="th")

    job = await _claim_next_job(pool, tenant_cap=100)
    await process_one_job(pool, LocalStorageProvider(), _manager(_FakeProvider()), job)

    sizes = [len(r["content"]) for r in await pool.fetch("SELECT content FROM kb_chunks WHERE document_id = $1", doc["id"])]
    assert len(sizes) > 1 and max(sizes) < len(text)


async def test_claim_cap_is_per_tenant_not_global(tenant_agent, other_tenant_admin, pool):
    tenant_a, _ = tenant_agent
    tenant_b = other_tenant_admin["tenant"]
    await _drain_pending(pool)
    try:
        kb_a = await _make_kb(tenant_a, "cap-a")
        kb_b = await _make_kb(tenant_b, "cap-b")
        doc_a1, doc_a2 = await _upload(tenant_a, kb_a, "a1"), await _upload(tenant_a, kb_a, "a2")
        doc_b = await _upload(tenant_b, kb_b, "b1")

        first = await _claim_next_job(pool, tenant_cap=1)
        second = await _claim_next_job(pool, tenant_cap=1)
        third = await _claim_next_job(pool, tenant_cap=1)

        assert first["document_id"] == doc_a1["id"]            # oldest job
        assert second["document_id"] == doc_b["id"]            # A is at its cap, B is not blocked
        assert third is None                                   # both tenants now at the cap
        assert doc_a2["ingestion_job_id"] != first["id"]
    finally:
        await _purge_tenant(pool, tenant_b["id"])


async def test_claim_skips_soft_deleted_documents(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "del-kb")
    doc = await _upload(tenant, kb, "gone")
    set_caller_tenant(str(tenant["id"]))
    await documents_service.soft_delete_document(doc["id"])

    assert await _claim_next_job(pool, tenant_cap=100) is None


async def test_reaper_event_carries_the_documents_own_tenant(tenant_agent, other_tenant_admin, pool):
    tenant_a, _ = tenant_agent
    tenant_b = other_tenant_admin["tenant"]
    await _drain_pending(pool)
    try:
        kb_a, kb_b = await _make_kb(tenant_a, "ra"), await _make_kb(tenant_b, "rb")
        doc_a, doc_b = await _upload(tenant_a, kb_a, "ra1"), await _upload(tenant_b, kb_b, "rb1")
        for _ in range(2):
            job = await _claim_next_job(pool, tenant_cap=100)
            await pool.execute(
                "UPDATE kb_ingestion_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = $1", job["id"],
            )

        assert await reap_expired_leases(pool) >= 2

        for doc, tenant in ((doc_a, tenant_a), (doc_b, tenant_b)):
            events = await _events(pool, doc["ingestion_job_id"])
            assert [e["status"] for e in events] == ["lease_expired"]
            assert events[0]["tenant_id"] == tenant["id"]
    finally:
        await _purge_tenant(pool, tenant_b["id"])


async def test_reaper_recovers_a_legacy_running_job_with_no_lease(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "legacy-kb")
    old, fresh = await _upload(tenant, kb, "old-run"), await _upload(tenant, kb, "fresh-run")
    # What a pre-lease worker leaves behind: running, no lease. One is old, one has just started.
    await pool.execute(
        "UPDATE kb_ingestion_jobs SET status = 'running', attempts = 1, lease_expires_at = NULL, "
        "started_at = now() - interval '1 hour' WHERE id = $1", old["ingestion_job_id"],
    )
    await pool.execute(
        "UPDATE kb_ingestion_jobs SET status = 'running', attempts = 1, lease_expires_at = NULL, "
        "started_at = now() WHERE id = $1", fresh["ingestion_job_id"],
    )

    await reap_expired_leases(pool)

    assert (await _job(pool, old["ingestion_job_id"]))["status"] == "pending"
    assert (await _job(pool, fresh["ingestion_job_id"]))["status"] == "running"  # grace period, may still be live


async def test_heartbeat_stops_extending_a_lease_it_no_longer_owns(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "hb-own-kb")
    doc = await _upload(tenant, kb, "owned")
    job = await _claim_next_job(pool, lease_s=2.0, tenant_cap=100)
    await pool.execute("UPDATE kb_ingestion_jobs SET attempts = attempts + 1 WHERE id = $1", job["id"])  # another attempt
    before = (await _job(pool, doc["ingestion_job_id"]))["lease_expires_at"]

    task = asyncio.create_task(_heartbeat(pool, job["id"], str(tenant["id"]), job["attempts"], 60.0, 0.05))
    await asyncio.wait_for(task, timeout=2.0)  # returns by itself once it sees the mismatch

    assert (await _job(pool, doc["ingestion_job_id"]))["lease_expires_at"] == before


async def test_events_are_invisible_to_another_tenant_under_rls(tenant_agent, other_tenant_admin, pool):
    tenant_a, _ = tenant_agent
    tenant_b = other_tenant_admin["tenant"]
    await _drain_pending(pool)
    kb = await _make_kb(tenant_a, "rls-ev-kb")
    doc = await _upload(tenant_a, kb, "rls-ev")
    job = await _claim_next_job(pool, tenant_cap=100)
    await process_one_job(pool, LocalStorageProvider(), _manager(_FakeProvider()), job)

    async def visible_to(tenant_id) -> int:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL ROLE yuviz_app")
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
                return await conn.fetchval("SELECT count(*) FROM kb_ingestion_events WHERE document_id = $1", doc["id"])

    assert await visible_to(tenant_a["id"]) == 5
    assert await visible_to(tenant_b["id"]) == 0
