"""An admin edit made while a re-ingest is running survives the chunk swap (a ready document stays editable)."""

from __future__ import annotations

import pytest

from libs.tenancy import set_caller_tenant
from services.knowledge import documents as documents_service
from services.knowledge import embedding_manager as em
from services.knowledge.ingestion_worker import _claim_next_job, process_one_job
from services.knowledge.reingest import requeue_documents
from services.knowledge.storage import LocalStorageProvider
from services.knowledge.tests.test_ingestion_queue import _drain_pending, _make_kb, _manager, _purge_tenant, _upload
from services.knowledge.tests.test_reindex_serving import _DOC


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_CHUNKER", "structure")
    monkeypatch.setattr(em, "EMBED_RETRY_BASE_S", 0.0)


class Editing:
    """Embedding provider that runs an admin action in the middle of the job (after the claim, before the swap)."""

    def __init__(self, action):
        self.action, self.done = action, False

    async def embed(self, texts):
        if not self.done:
            self.done = True
            await self.action()
        return [[0.1] * 768 for _ in texts]


async def _setup(pool, tenant, slug, content=_DOC):
    await _drain_pending(pool)
    kb = await _make_kb(tenant, slug)
    doc = await _upload(tenant, kb, "doc", content=content, content_type="text/markdown")
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    await process_one_job(pool, LocalStorageProvider(), _manager(Editing(_noop)), job)
    return kb, doc


async def _noop():
    return None


async def _reingest_with(pool, tenant, doc, action):
    set_caller_tenant(None)
    assert await requeue_documents(pool, tenant["id"]) == [str(doc["id"])]
    job = await _claim_next_job(pool, tenant_cap=100)
    await process_one_job(pool, LocalStorageProvider(), _manager(Editing(action)), job)
    return job


async def _row(pool, doc):
    return dict(await pool.fetchrow("SELECT * FROM kb_documents WHERE id = $1", doc["id"]))


@pytest.mark.parametrize("start,toggle_to", [("auto", "prompt"), ("prompt", "auto")])
async def test_usage_mode_toggled_mid_job_survives_in_both_directions(tenant_agent, pool, start, toggle_to):
    tenant, _ = tenant_agent
    kb, doc = await _setup(pool, tenant, f"swap-um-{start}-kb")
    set_caller_tenant(str(tenant["id"]))
    if start != "auto":
        await documents_service.update_document(doc["id"], usage_mode=start)

    async def toggle():
        set_caller_tenant(str(tenant["id"]))
        await documents_service.update_document(doc["id"], usage_mode=toggle_to)

    await _reingest_with(pool, tenant, doc, toggle)
    row = await _row(pool, doc)
    assert row["usage_mode"] == toggle_to and row["status"] == "ready"
    await _purge_tenant(pool, tenant["id"])


async def test_title_tags_and_language_edited_mid_job_survive_and_new_chunks_carry_the_new_language(tenant_agent, pool):
    tenant, _ = tenant_agent
    kb, doc = await _setup(pool, tenant, "swap-fields-kb")

    async def edit():
        set_caller_tenant(str(tenant["id"]))
        await documents_service.update_document(doc["id"], title="Renamed", tags={"team": "billing"}, language="hi")

    await _reingest_with(pool, tenant, doc, edit)
    row = await _row(pool, doc)
    assert row["title"] == "Renamed" and row["language"] == "hi" and '"billing"' in str(row["tags"])
    langs = {r["language"] for r in await pool.fetch("SELECT language FROM kb_chunks WHERE document_id = $1", doc["id"])}
    assert langs == {"hi"}
    await _purge_tenant(pool, tenant["id"])


async def test_a_document_deleted_mid_job_is_not_resurrected(tenant_agent, pool):
    tenant, _ = tenant_agent
    kb, doc = await _setup(pool, tenant, "swap-delete-kb")
    old = {r["id"] for r in await pool.fetch("SELECT id FROM kb_chunks WHERE document_id = $1", doc["id"])}

    async def delete():
        set_caller_tenant(str(tenant["id"]))
        await documents_service.soft_delete_document(doc["id"])

    job = await _reingest_with(pool, tenant, doc, delete)
    row = await _row(pool, doc)
    assert row["deleted_at"] is not None
    assert {r["id"] for r in await pool.fetch("SELECT id FROM kb_chunks WHERE document_id = $1", doc["id"])} == old   # nothing re-indexed
    j = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", job["id"]))
    assert j["status"] == "failed" and "deleted during ingestion" in j["error"] and j["error_class"] == "permanent"
    await _purge_tenant(pool, tenant["id"])


async def test_first_ingest_of_a_tiny_document_is_auto_inlined_but_a_reingest_respects_the_admin(tenant_agent, pool):
    tenant, _ = tenant_agent
    kb, doc = await _setup(pool, tenant, "swap-tiny-kb", content=b"We are closed on Sundays.")
    assert (await _row(pool, doc))["usage_mode"] == "prompt"           # first ingest: auto-inline
    set_caller_tenant(str(tenant["id"]))
    await documents_service.update_document(doc["id"], usage_mode="auto")   # the admin turns it off
    await _reingest_with(pool, tenant, doc, _noop)
    assert (await _row(pool, doc))["usage_mode"] == "auto"
    await _purge_tenant(pool, tenant["id"])
