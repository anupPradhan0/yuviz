"""Per-batch embedding retry and vector validation (pure), plus the worker storing parents, children and metadata."""

from __future__ import annotations

import math

import pytest

from services.knowledge import documents as documents_service
from services.knowledge import embedding_manager as em
from services.knowledge.embedding_manager import (
    EMBEDDING_DIMS, EmbeddingDimensionError, EmbeddingError, embed_in_batches,
)
from services.knowledge.ingestion_worker import classify_error, process_one_job
from services.knowledge.storage import LocalStorageProvider
from services.knowledge.tests.test_ingestion_queue import _make_kb, _manager, _purge_tenant, _upload

GOOD = [0.1] * EMBEDDING_DIMS


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(em, "EMBED_RETRY_BASE_S", 0.0)


class Scripted:
    """Records every batch it is asked for; `plan` maps call number -> a bad return value or an exception."""

    def __init__(self, plan: dict | None = None) -> None:
        self.plan = plan or {}
        self.batches: list[list[str]] = []

    async def embed(self, texts):
        self.batches.append(list(texts))
        out = self.plan.get(len(self.batches))
        if isinstance(out, Exception):
            raise out
        return out(texts) if callable(out) else [GOOD for _ in texts]


async def test_failed_batch_retries_alone_without_redoing_earlier_batches():
    p = Scripted({2: RuntimeError("timeout")})
    texts = [f"t{i}" for i in range(10)]
    stats: dict = {}
    vectors = await embed_in_batches(p, texts, batch_size=4, stats=stats)
    assert len(vectors) == 10
    # batch0 once, batch1 twice (fail, retry), batch2 once: batch0 was not redone
    assert [b[0] for b in p.batches] == ["t0", "t4", "t4", "t8"]
    assert stats == {"batches": 3, "batch_retries": 1}


async def test_batch_size_is_bounded():
    p = Scripted()
    await embed_in_batches(p, [str(i) for i in range(100)], batch_size=32)
    assert max(len(b) for b in p.batches) == 32 and len(p.batches) == 4


async def test_retries_are_bounded_and_the_last_error_surfaces():
    p = Scripted({n: RuntimeError("down") for n in range(1, 20)})
    with pytest.raises(RuntimeError, match="down"):
        await embed_in_batches(p, ["a"], retries=2)
    assert len(p.batches) == 3
    assert classify_error(RuntimeError("down")) == "transient"


@pytest.mark.parametrize("bad", [
    [math.nan] + [0.1] * (EMBEDDING_DIMS - 1),
    [math.inf] + [0.1] * (EMBEDDING_DIMS - 1),
    [0.0] * EMBEDDING_DIMS,
])
async def test_nan_inf_and_zero_vectors_are_transient_and_retried(bad):
    p = Scripted({1: lambda texts: [bad for _ in texts]})
    vectors = await embed_in_batches(p, ["a", "b"])
    assert vectors == [GOOD, GOOD] and len(p.batches) == 2  # first answer rejected, retry accepted

    always = Scripted({n: (lambda texts: [bad for _ in texts]) for n in range(1, 10)})
    with pytest.raises(EmbeddingError):
        await embed_in_batches(always, ["a"])
    assert classify_error(EmbeddingError("x")) == "transient"


async def test_wrong_count_is_transient():
    p = Scripted({1: lambda texts: [GOOD]})
    assert len(await embed_in_batches(p, ["a", "b"])) == 2 and len(p.batches) == 2


async def test_wrong_dimension_is_a_permanent_error_with_a_clear_message_and_no_retry():
    p = Scripted({1: lambda texts: [[0.1] * 1536 for _ in texts]})
    with pytest.raises(EmbeddingDimensionError, match=r"1536-dimensional.*vector\(768\)"):
        await embed_in_batches(p, ["a"])
    assert len(p.batches) == 1 and classify_error(EmbeddingDimensionError("x")) == "permanent"


# ── through the worker, against Postgres ────────────────────────────────

_DOC = ("# Handbook\n\n## Returns\n\n" + "\n\n".join(
    f"Paragraph {i}: refunds are processed within thirty days of the original purchase date provided the item is "
    f"returned in its original packaging with proof of purchase and a completed return form number {i}. " * 3
    for i in range(10)
) + "\n\n## Billing\n\nInvoices are emailed monthly and can be downloaded from the billing page at any time.\n").encode()


async def _ingest(pool, tenant, kb, provider, content=_DOC, title="handbook"):
    doc = await _upload(tenant, kb, title, content=content, content_type="text/markdown")
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    await process_one_job(pool, LocalStorageProvider(), _manager(provider), job)
    return doc


async def test_worker_stores_parents_children_and_embedding_metadata(tenant_agent, pool):
    from libs.tenancy import set_caller_tenant
    tenant, _ = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    kb = await _make_kb(tenant, "pc-kb")
    doc = await _ingest(pool, tenant, kb, Scripted())

    d = dict(await pool.fetchrow("SELECT * FROM kb_documents WHERE id = $1", doc["id"]))
    assert d["status"] == "ready" and d["chunker_version"] == "structure-v1" and d["parser_used"] == "text"

    parents = await pool.fetch("SELECT * FROM kb_chunks WHERE document_id = $1 AND is_parent", doc["id"])
    children = await pool.fetch("SELECT * FROM kb_chunks WHERE document_id = $1 AND NOT is_parent ORDER BY chunk_index", doc["id"])
    assert parents and children
    assert all(p["embedding"] is None and p["chunk_index"] < 0 and p["parent_id"] is None for p in parents)
    assert len({p["chunk_index"] for p in parents}) == len(parents)
    assert all(c["embedding"] is not None and c["chunk_index"] >= 0 for c in children)
    assert all(c["embedding_model"] == "nomic-embed-text" and c["dims"] == 768 for c in children)
    assert all(p["embedding_model"] is None for p in parents)
    parent_ids = {p["id"] for p in parents}
    linked = [c for c in children if c["parent_id"] is not None]
    assert linked and all(c["parent_id"] in parent_ids for c in linked)
    for c in linked:  # the linked parent is the enclosing section of the same document
        parent = next(p for p in parents if p["id"] == c["parent_id"])
        assert parent["section_path"] == c["section_path"]
    assert any(c["section_path"] == "Handbook > Returns" and c["content"].startswith("Handbook > Returns") for c in children)

    listed = next(x for x in await documents_service.list_documents(kb["id"]) if x["id"] == doc["id"])
    assert listed["chunk_count"] == len(children)  # parents are not counted as retrievable chunks
    await _purge_tenant(pool, tenant["id"])


async def test_vector_search_ignores_parent_rows(tenant_agent, pool):
    from libs.tenancy import tenant_conn
    from services.knowledge.vector_repository import PgVectorRepository

    tenant, _ = tenant_agent
    kb = await _make_kb(tenant, "vs-kb")
    # A direction no other test uses: the HNSW index is shared by every row in the database, and its candidate list
    # (ef_search) is cut before the kb filter, so identical vectors from other tests could crowd this one out.
    one_hot = [1.0 if i == 700 else 0.0 for i in range(EMBEDDING_DIMS)]
    doc = await _ingest(pool, tenant, kb, Scripted({n: (lambda t: [one_hot for _ in t]) for n in range(1, 30)}))
    # Even a parent row that somehow carried an embedding must not be returned.
    await pool.execute(
        "UPDATE kb_chunks SET embedding = $2::vector WHERE document_id = $1 AND is_parent",
        doc["id"], "[" + ",".join(str(x) for x in one_hot) + "]",
    )
    async with tenant_conn(pool, explicit_tenant=str(tenant["id"]), reason="test") as conn:
        matches = await PgVectorRepository().search(conn, [str(kb["id"])], one_hot, 100, 0.0)
    parent_ids = {str(r["id"]) for r in await pool.fetch("SELECT id FROM kb_chunks WHERE document_id = $1 AND is_parent", doc["id"])}
    status = await pool.fetchrow("SELECT status, error FROM kb_documents WHERE id = $1", doc["id"])
    assert matches and not ({m.chunk_id for m in matches} & parent_ids), dict(status)
    await _purge_tenant(pool, tenant["id"])


async def test_worker_retries_a_bad_batch_in_place_and_records_it(tenant_agent, pool):
    tenant, _ = tenant_agent
    kb = await _make_kb(tenant, "retry2-kb")
    flaky = Scripted({1: RuntimeError("provider hiccup")})
    doc = await _ingest(pool, tenant, kb, flaky)
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    assert job["status"] == "succeeded" and job["attempts"] == 1  # no job-level retry was needed
    detail = await pool.fetchval(
        "SELECT detail FROM kb_ingestion_events WHERE job_id = $1 AND stage = 'embed' AND status = 'ok'", job["id"],
    )
    assert '"batch_retries": 1' in detail
    await _purge_tenant(pool, tenant["id"])


async def test_wrong_dimension_fails_the_document_permanently_with_a_clear_reason(tenant_agent, pool):
    tenant, _ = tenant_agent
    kb = await _make_kb(tenant, "dim-kb")
    wrong = Scripted({n: (lambda texts: [[0.1] * 1536 for _ in texts]) for n in range(1, 50)})
    doc = await _ingest(pool, tenant, kb, wrong)
    d = dict(await pool.fetchrow("SELECT * FROM kb_documents WHERE id = $1", doc["id"]))
    j = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    assert d["status"] == "failed" and "1536-dimensional" in d["error"] and "vector(768)" in d["error"]
    assert j["status"] == "failed" and j["error_class"] == "permanent" and j["attempts"] == 1
    await _purge_tenant(pool, tenant["id"])
