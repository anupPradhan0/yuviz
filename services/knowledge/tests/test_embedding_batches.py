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
    monkeypatch.setenv("KNOWLEDGE_CHUNKER", "structure")  # these tests check parents/children/section_path


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
    assert len(vectors) == 2 and vectors[0][0] == pytest.approx(0.1) and len(vectors[1]) == 768 and len(p.batches) == 2  # first answer rejected, retry accepted

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
    from services.knowledge.tests.rag_fakes import add_document, embed_text, vec_literal
    from services.knowledge.vector_repository import PgVectorRepository

    tenant, _ = tenant_agent
    kb = await _make_kb(tenant, "vs-kb")
    # Rows inserted directly: this test is about which rows the search SQL admits (NOT is_parent), so it must not
    # depend on the ingestion worker, which a background worker task left over from another test could race.
    text = "zyxwv unique parent row probe sentence"
    doc = await add_document(pool, tenant, kb, "Probe", [text], parent="Section: " + text + " and the rest of its section")
    vector = embed_text(text)
    # Even a parent row that somehow carried an embedding must not be returned.
    await pool.execute(
        "UPDATE kb_chunks SET embedding = $2::vector WHERE document_id = $1 AND is_parent", doc["id"], vec_literal(vector))
    async with tenant_conn(pool, explicit_tenant=str(tenant["id"]), reason="test") as conn:
        # Exact scan inside this transaction: the approximate HNSW index is shared with every other test's rows,
        # whose neighbours can crowd the candidate list (ef_search) before the kb filter.
        await conn.execute("SET LOCAL enable_indexscan = off")
        matches = await PgVectorRepository().search(conn, [str(kb["id"])], vector, 100, 0.0)
    assert [m.chunk_id for m in matches] == doc["chunk_ids"], "the child must be found"
    assert doc["parent_id"] not in {m.chunk_id for m in matches}
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


# ── R10: permanent provider errors are not retried; the shipped retry default works end to end ──

import httpx  # noqa: E402

from services.knowledge.embedding_manager import EmbeddingRequestRejected  # noqa: E402


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://user:secret@embed.example.com/v1/embeddings?key=sk-live")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(status, request=req))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_permanent_client_errors_are_not_retried(status):
    p = Scripted({n: _http_error(status) for n in range(1, 20)})
    with pytest.raises(EmbeddingRequestRejected, match=f"HTTP {status}") as e:
        await embed_in_batches(p, ["a", "b"])
    assert len(p.batches) == 1 and classify_error(e.value) == "permanent"
    assert "secret" not in str(e.value) and "sk-live" not in str(e.value)


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
async def test_rate_limits_timeouts_and_server_errors_are_retried(status):
    p = Scripted({1: _http_error(status), 2: _http_error(status)})
    assert len(await embed_in_batches(p, ["a"])) == 1 and len(p.batches) == 3
    always = Scripted({n: _http_error(status) for n in range(1, 20)})
    with pytest.raises(httpx.HTTPStatusError):
        await embed_in_batches(always, ["a"])
    assert len(always.batches) == em.EMBED_BATCH_RETRIES + 1 and classify_error(_http_error(status)) == "transient"


def test_shipped_retry_defaults_are_what_the_docs_say():
    assert (em.EMBED_BATCH_RETRIES, em.EMBED_BATCH_SIZE) == (2, 32)


async def test_worker_with_the_shipped_default_retries_survives_two_provider_errors(tenant_agent, pool):
    """No monkeypatch of EMBED_BATCH_RETRIES here: the Phase 1 tests pin it to 0, so this is what production runs."""
    tenant, _ = tenant_agent
    kb = await _make_kb(tenant, "default-retry-kb")
    flaky = Scripted({1: _http_error(503), 2: _http_error(429)})
    doc = await _ingest(pool, tenant, kb, flaky)
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    assert job["status"] == "succeeded" and job["attempts"] == 1
    assert (await pool.fetchval("SELECT status FROM kb_documents WHERE id = $1", doc["id"])) == "ready"
    await _purge_tenant(pool, tenant["id"])


async def test_worker_fails_a_misconfigured_provider_after_one_call_per_batch(tenant_agent, pool):
    tenant, _ = tenant_agent
    kb = await _make_kb(tenant, "bad-key-kb")
    bad = Scripted({n: _http_error(401) for n in range(1, 50)})
    doc = await _ingest(pool, tenant, kb, bad)
    j = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    d = dict(await pool.fetchrow("SELECT * FROM kb_documents WHERE id = $1", doc["id"]))
    assert j["status"] == "failed" and j["error_class"] == "permanent" and j["attempts"] == 1
    assert d["status"] == "failed" and "HTTP 401" in d["error"] and "secret" not in d["error"]
    assert len(bad.batches) == 1
    await _purge_tenant(pool, tenant["id"])


async def test_chunker_version_records_the_strategy_that_ran(tenant_agent, pool, monkeypatch):
    from services.knowledge import chunker
    tenant, _ = tenant_agent
    kb = await _make_kb(tenant, "version-kb")
    intro = (b"This handbook explains how Acme handles returns for retail customers in every region we serve today "
             b"and is reviewed once a year by the policy team.\n\n# Returns\n\n" + b"Items can be returned within thirty days. " * 12)
    ok = await _ingest(pool, tenant, kb, Scripted(), content=intro, title="intro")
    assert await pool.fetchval("SELECT chunker_version FROM kb_documents WHERE id = $1", ok["id"]) == "structure-v1"
    assert await pool.fetchval("SELECT count(*) FROM kb_chunks WHERE document_id = $1 AND section_path IS NOT NULL", ok["id"]) >= 1

    def boom(*a, **k):
        raise RuntimeError("splitter bug")
    monkeypatch.setattr(chunker, "_build_sections", boom)
    fb = await _ingest(pool, tenant, kb, Scripted(), content=intro, title="fallback")
    assert await pool.fetchval("SELECT chunker_version FROM kb_documents WHERE id = $1", fb["id"]) == "fixed-v1"
    await _purge_tenant(pool, tenant["id"])
