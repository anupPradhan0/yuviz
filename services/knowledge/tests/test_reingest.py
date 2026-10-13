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
