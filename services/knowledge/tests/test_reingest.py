"""Strategy change reaches existing documents through the normal queue, tenant-scoped."""

from __future__ import annotations

import pytest

from libs.tenancy import set_caller_tenant

from services.knowledge import chunker
from services.knowledge.ingestion_worker import _claim_next_job, process_one_job
from services.knowledge.reingest import main as reingest_main
from services.knowledge.reingest import requeue_documents
from services.knowledge.storage import LocalStorageProvider
from services.knowledge.tests.test_ingestion_queue import (
    _FakeProvider, _drain_pending, _make_kb, _manager, _purge_tenant, _upload,
)

_DOC = ("# Handbook\n\n## Returns\n\n" + "\n\n".join(
    f"Paragraph {i}: refunds are processed within thirty days of the original purchase date provided the item is "
    f"returned in its original packaging with proof of purchase and a completed return form number {i}. " * 3
    for i in range(10)) + "\n\n## Billing\n\nInvoices are emailed monthly.\n").encode()


async def _run_all_pending(pool, tenant_id):
    done = 0
    while (job := await _claim_next_job(pool, tenant_cap=100)) is not None:
        await process_one_job(pool, LocalStorageProvider(), _manager(_FakeProvider()), job)
        done += 1
    return done


async def _ingest(pool, tenant, kb, title):
    doc = await _upload(tenant, kb, title, content=_DOC, content_type="text/markdown")
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    await process_one_job(pool, LocalStorageProvider(), _manager(_FakeProvider()), job)
    return doc


async def _row(pool, doc_id):
    return dict(await pool.fetchrow("SELECT * FROM kb_documents WHERE id = $1", doc_id))


async def test_a_strategy_change_reaches_existing_documents_through_the_queue(tenant_agent, pool, monkeypatch):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "reingest-kb")
    monkeypatch.setenv("KNOWLEDGE_CHUNKER", "structure")
    doc = await _ingest(pool, tenant, kb, "handbook")
    before = await _row(pool, doc["id"])
    assert before["chunker_version"] == "structure-v1"
    old_chunks = {r["id"] for r in await pool.fetch("SELECT id FROM kb_chunks WHERE document_id = $1", doc["id"])}
    assert any(r["is_parent"] for r in await pool.fetch("SELECT is_parent FROM kb_chunks WHERE document_id = $1", doc["id"]))

    monkeypatch.setenv("KNOWLEDGE_CHUNKER", "fixed")
    set_caller_tenant(None)
    assert await requeue_documents(pool, tenant["id"], dry_run=True) == [str(doc["id"])]
    assert (await _row(pool, doc["id"]))["version"] == before["version"]          # dry run changes nothing
    assert await requeue_documents(pool, tenant["id"], kb["id"]) == [str(doc["id"])]
    mid = await _row(pool, doc["id"])
    assert mid["version"] == before["version"] + 1 and mid["status"] == "ready"   # bumped, still served meanwhile
    assert {r["id"] for r in await pool.fetch("SELECT id FROM kb_chunks WHERE document_id = $1", doc["id"])} == old_chunks

    assert await _run_all_pending(pool, tenant["id"]) == 1
    after = await _row(pool, doc["id"])
    assert after["chunker_version"] == "fixed-v1" and after["status"] == "ready"
    rows = await pool.fetch("SELECT id, is_parent, version FROM kb_chunks WHERE document_id = $1", doc["id"])
    assert rows and not ({r["id"] for r in rows} & old_chunks)                    # chunks were swapped
    assert not any(r["is_parent"] for r in rows) and {r["version"] for r in rows} == {after["version"]}
    await _purge_tenant(pool, tenant["id"])


async def test_requeue_skips_documents_with_an_active_job_and_ones_already_at_the_target_version(tenant_agent, pool, monkeypatch):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "reingest-skip-kb")
    monkeypatch.setenv("KNOWLEDGE_CHUNKER", "fixed")
    done = await _ingest(pool, tenant, kb, "done")
    pending = await _upload(tenant, kb, "pending", content=_DOC, content_type="text/markdown")  # job still queued
    await pool.execute("UPDATE kb_documents SET status = 'ready' WHERE id = $1", pending["id"])
    set_caller_tenant(None)
    assert await requeue_documents(pool, tenant["id"], except_version="fixed-v1") == []   # done is already fixed-v1
    assert await requeue_documents(pool, tenant["id"]) == [str(done["id"])]               # pending has an active job
    assert await requeue_documents(pool, tenant["id"]) == []                              # now it has one too: no duplicate
    assert await pool.fetchval("SELECT count(*) FROM kb_ingestion_jobs WHERE document_id = $1", done["id"]) == 2
    await _purge_tenant(pool, tenant["id"])


async def test_requeue_never_touches_another_tenants_documents_or_failed_ones(tenant_agent, other_tenant_admin, pool, monkeypatch):
    tenant, _ = tenant_agent
    other = other_tenant_admin["tenant"]
    await _drain_pending(pool)
    kb, other_kb = await _make_kb(tenant, "mine-kb"), await _make_kb(other, "theirs-kb")
    monkeypatch.setenv("KNOWLEDGE_CHUNKER", "fixed")
    mine, theirs = await _ingest(pool, tenant, kb, "mine"), await _ingest(pool, other, other_kb, "theirs")
    failed = await _ingest(pool, tenant, kb, "failed")
    await pool.execute("UPDATE kb_documents SET status = 'failed' WHERE id = $1", failed["id"])

    set_caller_tenant(None)  # the operator CLI has no caller tenant; it names one explicitly
    assert await requeue_documents(pool, tenant["id"]) == [str(mine["id"])]
    assert (await _row(pool, theirs["id"]))["version"] == 1
    assert await pool.fetchval("SELECT count(*) FROM kb_ingestion_jobs WHERE document_id = $1", theirs["id"]) == 1
    # naming the other tenant's KB under this tenant's scope matches nothing (RLS + tenant predicate)
    assert await requeue_documents(pool, tenant["id"], other_kb["id"]) == []
    assert await requeue_documents(pool, other["id"], kb["id"]) == []
    await _purge_tenant(pool, tenant["id"])
    await _purge_tenant(pool, other["id"])


def test_cli_requires_a_tenant():
    with pytest.raises(SystemExit):
        reingest_main([])


# ── review follow-ups: concurrent runs, fairness, batching ──────────────

async def _bulk_documents(pool, tenant, kb, n):
    """n ready documents, inserted directly (the queue behaviour is what is under test, not parsing)."""
    ids = []
    for i in range(n):
        row = await pool.fetchrow(
            "INSERT INTO kb_documents (kb_id, tenant_id, title, source_ref, content_type, status, chunker_version) "
            "VALUES ($1, $2, $3, 'x', 'text/plain', 'ready', 'structure-v1') RETURNING id", kb["id"], tenant["id"], f"d{i}")
        ids.append(row["id"])
    return ids


async def test_overlapping_runs_never_queue_a_document_twice(tenant_agent, pool):
    import asyncio
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "race-kb")
    ids = await _bulk_documents(pool, tenant, kb, 40)
    set_caller_tenant(None)
    results = await asyncio.gather(*[requeue_documents(pool, tenant["id"], batch=0) for _ in range(8)])
    queued = [d for r in results for d in r]
    assert sorted(queued) == sorted(str(i) for i in ids)                       # each document queued by exactly one run
    assert await pool.fetchval("SELECT count(*) FROM kb_ingestion_jobs WHERE document_id = ANY($1)", ids) == 40
    assert await pool.fetchval("SELECT max(version) FROM kb_documents WHERE id = ANY($1)", ids) == 2   # bumped once
    await _purge_tenant(pool, tenant["id"])


async def test_a_fresh_upload_is_claimed_before_the_whole_tenant_wide_reingest(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "fair-kb")
    await _bulk_documents(pool, tenant, kb, 200)
    set_caller_tenant(None)
    assert len(await requeue_documents(pool, tenant["id"], batch=0)) == 200
    fresh = await _upload(tenant, kb, "fresh", content=_DOC, content_type="text/markdown")   # queued AFTER the 200
    first = await _claim_next_job(pool, tenant_cap=100)
    assert first["id"] == fresh["ingestion_job_id"]
    order = [first["document_id"]]
    for _ in range(5):                                            # the rest is the re-ingest backlog, oldest first
        order.append((await _claim_next_job(pool, tenant_cap=100))["document_id"])
    assert fresh["id"] == order[0] and fresh["id"] not in order[1:]
    await pool.execute("UPDATE kb_ingestion_jobs SET status = 'succeeded' WHERE document_id = ANY($1)", order)
    await _purge_tenant(pool, tenant["id"])


async def test_batch_limits_each_run_and_repeated_runs_walk_through_every_document(tenant_agent, pool):
    tenant, _ = tenant_agent
    await _drain_pending(pool)
    kb = await _make_kb(tenant, "batch-kb")
    ids = await _bulk_documents(pool, tenant, kb, 25)
    set_caller_tenant(None)
    seen: list[str] = []
    for _ in range(4):
        got = await requeue_documents(pool, tenant["id"], batch=10)
        assert len(got) <= 10 and not (set(got) & set(seen))
        seen += got
    assert sorted(seen) == sorted(str(i) for i in ids)
    assert len(await requeue_documents(pool, tenant["id"], batch=10, dry_run=True)) == 0     # all have active jobs
    await _purge_tenant(pool, tenant["id"])
