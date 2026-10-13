"""Re-embed migration: shadow column, resumability, zero downtime, atomic swap, ingest during the migration.
Real Postgres; per-config fake embedders whose vector spaces differ (each word renamed per space), so "which space is this vector
in" is checkable by recomputing it."""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio
from rag_fakes import add_document, embed_text, make_kb, vec_literal

from libs.tenancy import set_caller_tenant
from services.config import provider_configs
from services.knowledge import audit, embedding_manager, reembed
from services.knowledge import documents as documents_service
from services.knowledge import retrieval
from services.knowledge.embedding_manager import EmbeddingProviderManager
from services.knowledge.ingestion_worker import process_one_job
from services.knowledge.secret_resolver import CompositeSecretResolver
from services.knowledge.storage import LocalStorageProvider
from services.knowledge.vector_repository import PgVectorRepository

pytestmark = pytest.mark.usefixtures("fast_retries")

TEXTS = [f"Policy number {i} says that widget class {i} ships within {i + 2} days of the order" for i in range(7)]


@pytest_asyncio.fixture(loop_scope="session", scope="module", autouse=True)
async def _vacuum_after_module():
    """The swap rewrites every vector, leaving dead entries in the HNSW graph until vacuum; with them still there the
    approximate search of a LATER module can return fewer rows than exist (the same effect docs/knowledge-phase7.md
    tells operators to clear with a REINDEX after a big swap)."""
    yield
    from services.knowledge import db
    await (await db.get_pool()).execute("VACUUM kb_chunks")


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(embedding_manager, "EMBED_RETRY_BASE_S", 0.0)


class Space:
    """An embedder whose vectors live in its own space: embed_text of the text behind a prefix word."""

    def __init__(self, prefix: str, *, fail_on_call: int | None = None, dims: int = 768, latency: float = 0.0):
        self.prefix, self.fail_on_call, self.dims, self.latency = prefix, fail_on_call, dims, latency
        self.calls = 0
        self.texts = 0
        self.hook = None  # async callable run inside embed(): lets a test act "while the provider is working"

    def vec(self, text: str) -> list[float]:
        return embed_text(" ".join(f"{self.prefix}{w}" for w in text.split()))

    async def embed(self, texts):
        self.calls += 1
        if self.hook is not None:
            await self.hook()
        if self.latency:
            await asyncio.sleep(self.latency)
        if self.fail_on_call is not None and self.calls >= self.fail_on_call:
            raise RuntimeError("provider down")
        self.texts += len(texts)
        vectors = [self.vec(t) for t in texts]
        return [v[: self.dims] if self.dims <= 768 else v + [0.1] * (self.dims - 768) for v in vectors]


def _manager(spaces: dict[str, Space]) -> EmbeddingProviderManager:
    async def factory(cfg, api_key):
        return spaces[cfg.id]
    return EmbeddingProviderManager(CompositeSecretResolver(), registry={"ollama": factory})


async def _config(tenant, name: str, model: str):
    set_caller_tenant(str(tenant["id"]))
    return await provider_configs.create_provider_config(
        tenant_id=tenant["id"], name=name, role="embedding", engine="ollama", model=model, allow_pointer_schemes=False)


@pytest_asyncio.fixture(loop_scope="session")
async def mig(pool, tenant_agent):
    """A KB embedded in space v1 (config A, model m-a) with three documents, parents on two of the chunks, and a
    second embedding config B (model m-b) to migrate to."""
    tenant, agent = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    kb = await make_kb(pool, tenant, agent, "mig")
    await pool.execute("UPDATE provider_configs SET model = 'm-a' WHERE id = $1", kb["embedding_config_id"])
    v1 = Space("v1")
    docs = [
        await add_document(pool, tenant, kb, "Doc A", TEXTS[:3], parent="Section A: " + " ".join(TEXTS[:3])),
        await add_document(pool, tenant, kb, "Doc B", TEXTS[3:5]),
        await add_document(pool, tenant, kb, "Doc C", TEXTS[5:]),
    ]
    for t_idx, row in enumerate(await pool.fetch("SELECT id, content FROM kb_chunks WHERE kb_id = $1 AND NOT is_parent", kb["id"])):
        await pool.execute("UPDATE kb_chunks SET embedding = $2::vector, embedding_model = 'm-a' WHERE id = $1",
                           row["id"], vec_literal(v1.vec(row["content"])))
    cfg_b = await _config(tenant, "Embed-B", "m-b")
    cfg_c = await _config(tenant, "Embed-C", "m-c")
    return {"tenant": tenant, "agent": agent, "kb": kb, "docs": docs, "a": str(kb["embedding_config_id"]),
            "b": str(cfg_b["id"]), "c": str(cfg_c["id"]), "v1": v1}


def _spaces(m, v2=None, v3=None):
    return {m["a"]: m["v1"], m["b"]: v2 or Space("v2"), m["c"]: v3 or Space("v3")}


async def _run(pool, m, target, spaces=None, **kw):
    set_caller_tenant(None)
    return await reembed.reembed_kb(
        pool, _manager(spaces or _spaces(m)), tenant_id=str(m["tenant"]["id"]), kb_id=m["kb"]["id"],
        to_config=m[target], **kw)


async def _state(pool, m):
    kb = await pool.fetchrow("SELECT embedding_config_id FROM knowledge_bases WHERE id = $1", m["kb"]["id"])
    chunks = await pool.fetch(
        "SELECT id, content, embedding::text AS emb, embedding_model, embedding_next IS NOT NULL AS has_next, "
        "embedding_next_model, is_parent FROM kb_chunks WHERE kb_id = $1", m["kb"]["id"])
    return str(kb["embedding_config_id"]), chunks


def _in_space(space: Space, row) -> bool:
    got = [float(x) for x in row["emb"].strip("[]").split(",")]
    want = space.vec(row["content"])
    return max(abs(a - b) for a, b in zip(got, want)) < 1e-5


# --- the swap ----------------------------------------------------------------------------------------------------

async def test_migration_moves_every_chunk_to_the_new_space_and_the_kb_to_the_new_config(pool, mig):
    v2 = Space("v2")
    report = await _run(pool, mig, "b", _spaces(mig, v2), batch=3)
    assert report.swapped and report.embedded == 7 and report.total == 7
    cfg, chunks = await _state(pool, mig)
    assert cfg == mig["b"]
    children = [c for c in chunks if not c["is_parent"]]
    assert len(children) == 7 and all(_in_space(v2, c) and c["embedding_model"] == "m-b" for c in children)
    assert all(not c["has_next"] and c["embedding_next_model"] is None for c in chunks)  # shadow cleared
    parent = next(c for c in chunks if c["is_parent"])
    assert parent["emb"] is None  # parents never carry vectors


async def test_retrieval_works_before_and_after_and_dense_search_uses_the_hnsw_index_in_the_new_space(pool, mig):
    spaces = _spaces(mig)
    set_caller_tenant(str(mig["tenant"]["id"]))
    before = await retrieval.retrieve(
        None, PgVectorRepository(), _manager(spaces), tenant_slug=mig["tenant"]["slug"], agent_slug="sup",
        query=TEXTS[4], hybrid_search=False, minimum_score=0.0)
    assert before["status"] == "ok" and TEXTS[4] in before["chunks"][0]["content"]
    assert (await _run(pool, mig, "b", spaces)).swapped
    retrieval.clear_caches()
    set_caller_tenant(str(mig["tenant"]["id"]))
    after = await retrieval.retrieve(
        None, PgVectorRepository(), _manager(spaces), tenant_slug=mig["tenant"]["slug"], agent_slug="sup",
        query=TEXTS[4], hybrid_search=False, minimum_score=0.0)
    assert after["status"] == "ok" and TEXTS[4] in after["chunks"][0]["content"] and after["chunks"][0]["score"] > 0.99
    # The new-space vector only matches through the swapped column: the old model's query finds nothing close.
    retrieval.clear_caches()
    wrong = await retrieval.retrieve(
        None, PgVectorRepository(), _manager({**spaces, mig["b"]: Space("v1")}), tenant_slug=mig["tenant"]["slug"],
        agent_slug="sup", query=TEXTS[4], hybrid_search=False, minimum_score=0.9)
    assert wrong["status"] == "no_match"


async def test_swap_is_atomic_when_it_fails_midway(pool, mig, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("audit down")
    monkeypatch.setattr(audit, "write_audit", boom)
    with pytest.raises(RuntimeError):
        await _run(pool, mig, "b")
    cfg, chunks = await _state(pool, mig)
    children = [c for c in chunks if not c["is_parent"]]
    assert cfg == mig["a"] and all(_in_space(mig["v1"], c) and c["embedding_model"] == "m-a" for c in children)
    assert all(c["has_next"] for c in children)  # the filled shadow survived the rollback: the next run only swaps
    monkeypatch.undo()
    report = await _run(pool, mig, "b")
    assert report.swapped and report.embedded == 0 and report.already_done == 7


async def test_a_reader_snapshot_sees_all_old_or_all_new_never_a_mix(pool, mig):
    async with pool.acquire() as reader:
        tx = reader.transaction(isolation="repeatable_read")
        await tx.start()
        await reader.fetchval("SELECT 1")  # take the snapshot
        report = await _run(pool, mig, "b")
        assert report.swapped
        rows = await reader.fetch("SELECT content, embedding::text AS emb FROM kb_chunks WHERE kb_id = $1 AND NOT is_parent", mig["kb"]["id"])
        cfg = await reader.fetchval("SELECT embedding_config_id FROM knowledge_bases WHERE id = $1", mig["kb"]["id"])
        await tx.rollback()
    assert str(cfg) == mig["a"] and all(_in_space(mig["v1"], r) for r in rows)


# --- zero downtime -----------------------------------------------------------------------------------------------

async def test_readers_always_find_chunks_with_consistent_vectors_while_migrating(pool, mig):
    spaces = _spaces(mig, Space("v2", latency=0.03))
    outcomes: list[tuple[str, str]] = []
    stop = asyncio.Event()

    async def reader():
        i = 0
        while not stop.is_set():
            i += 1
            set_caller_tenant(str(mig["tenant"]["id"]))
            r = await retrieval.retrieve(
                None, PgVectorRepository(), _manager(spaces), tenant_slug=mig["tenant"]["slug"], agent_slug="sup",
                query=TEXTS[i % 7], hybrid_search=False, minimum_score=0.0, top_k=1)
            outcomes.append((r["status"], r["chunks"][0]["content"] if r["chunks"] else ""))
            await asyncio.sleep(0.005)

    task = asyncio.create_task(reader())
    report = await _run(pool, mig, "b", spaces, batch=2)
    await asyncio.sleep(0.1)
    stop.set()
    await task
    assert report.swapped and len(outcomes) > 10
    assert all(s == "ok" for s, _ in outcomes), outcomes
    # every answer is the chunk that was asked for: no query ever met a vector from the other space
    assert all(any(t in content for t in TEXTS) for _, content in outcomes)


async def test_queries_keep_using_the_old_config_until_the_swap(pool, mig):
    seen = {}

    async def spy_cfg():
        seen["cfg"] = str(await pool.fetchval("SELECT embedding_config_id FROM knowledge_bases WHERE id = $1", mig["kb"]["id"]))
        seen["old_vectors"] = await pool.fetchval(
            "SELECT count(*) FROM kb_chunks WHERE kb_id = $1 AND embedding_model = 'm-a' AND NOT is_parent", mig["kb"]["id"])

    v2 = Space("v2")
    v2.hook = spy_cfg
    await _run(pool, mig, "b", _spaces(mig, v2), batch=3)
    assert seen == {"cfg": mig["a"], "old_vectors": 7}  # the last batch ran with the KB still entirely on the old model


# --- resumable ---------------------------------------------------------------------------------------------------

async def test_an_interrupted_run_resumes_where_it_stopped(pool, mig):
    v2 = Space("v2")
    first = await _run(pool, mig, "b", _spaces(mig, v2), batch=2, max_batches=2)
    assert not first.swapped and first.reason == "stopped" and first.embedded == 4
    cfg, chunks = await _state(pool, mig)
    assert cfg == mig["a"] and sum(c["has_next"] for c in chunks) == 4
    assert all(_in_space(mig["v1"], c) for c in chunks if not c["is_parent"])  # queries still see only the old space
    v2b = Space("v2")
    second = await _run(pool, mig, "b", _spaces(mig, v2b), batch=2)
    assert second.swapped and second.already_done == 4 and second.embedded == 3 and v2b.texts == 3


async def test_retargeting_re_embeds_what_the_previous_target_had_done(pool, mig):
    await _run(pool, mig, "b", batch=2, max_batches=2)
    v3 = Space("v3")
    report = await _run(pool, mig, "c", _spaces(mig, v3=v3), batch=4)
    assert report.swapped and report.already_done == 0 and report.embedded == 7
    cfg, chunks = await _state(pool, mig)
    assert cfg == mig["c"] and all(_in_space(v3, c) and c["embedding_model"] == "m-c" for c in chunks if not c["is_parent"])


async def test_abandon_clears_the_shadow_and_leaves_the_kb_alone(pool, mig):
    await _run(pool, mig, "b", batch=2, max_batches=1)
    assert await reembed.abandon(pool, str(mig["tenant"]["id"]), mig["kb"]["id"]) == 2
    cfg, chunks = await _state(pool, mig)
    assert cfg == mig["a"] and not any(c["has_next"] for c in chunks)


# --- failures leave the KB on the old model ------------------------------------------------------------------------

async def test_provider_failure_midway_leaves_the_kb_fully_on_the_old_model(pool, mig):
    v2 = Space("v2", fail_on_call=3)
    with pytest.raises(RuntimeError):
        await _run(pool, mig, "b", _spaces(mig, v2), batch=2)
    cfg, chunks = await _state(pool, mig)
    children = [c for c in chunks if not c["is_parent"]]
    assert cfg == mig["a"] and all(_in_space(mig["v1"], c) and c["embedding_model"] == "m-a" for c in children)
    assert sum(c["has_next"] for c in children) == 4  # progress kept (batches 1-2), nothing half-written
    done = await _run(pool, mig, "b")
    assert done.swapped and done.already_done == 4 and done.embedded == 3


async def test_dimension_mismatch_is_refused_before_anything_is_stored(pool, mig):
    for dims in (1536, 512):
        with pytest.raises(embedding_manager.EmbeddingDimensionError):
            await _run(pool, mig, "b", _spaces(mig, Space("v2", dims=dims)))
    cfg, chunks = await _state(pool, mig)
    assert cfg == mig["a"] and not any(c["has_next"] for c in chunks)


async def test_nan_and_zero_vectors_are_refused(pool, mig):
    class Bad(Space):
        async def embed(self, texts):
            return [[float("nan")] * 768 for _ in texts] if self.prefix == "nan" else [[0.0] * 768 for _ in texts]

    for prefix in ("nan", "zero"):
        with pytest.raises(embedding_manager.EmbeddingError):
            await _run(pool, mig, "b", _spaces(mig, Bad(prefix)))
    _, chunks = await _state(pool, mig)
    assert not any(c["has_next"] for c in chunks)


async def test_dry_run_calls_no_provider_and_writes_nothing(pool, mig):
    v2 = Space("v2")
    report = await _run(pool, mig, "b", _spaces(mig, v2), dry_run=True)
    assert report.reason == "dry_run" and report.total == 7 and report.embedded == 0 and v2.calls == 0
    _, chunks = await _state(pool, mig)
    assert not any(c["has_next"] for c in chunks)


async def test_migrating_to_the_current_config_is_a_no_op(pool, mig):
    v1 = mig["v1"]
    calls = v1.calls
    report = await _run(pool, mig, "a")
    assert report.reason == "already_on_target" and not report.swapped and v1.calls == calls


async def test_unknown_kb_or_config_is_refused(pool, mig):
    set_caller_tenant(None)
    with pytest.raises(ValueError):
        await reembed.reembed_kb(pool, _manager({}), tenant_id=str(mig["tenant"]["id"]), kb_id=uuid.uuid4(), to_config=mig["b"])
    with pytest.raises(ValueError):
        await reembed.reembed_kb(pool, _manager({}), tenant_id=str(mig["tenant"]["id"]), kb_id=mig["kb"]["id"], to_config=uuid.uuid4())


async def test_other_kbs_of_the_tenant_are_untouched(pool, mig):
    other = await make_kb(pool, mig["tenant"], mig["agent"], "other")
    await add_document(pool, mig["tenant"], other, "Other", ["unrelated text about gardening"])
    await _run(pool, mig, "b")
    row = await pool.fetchrow("SELECT embedding_model, embedding_next IS NULL AS clean FROM kb_chunks WHERE kb_id = $1", other["id"])
    assert row["embedding_model"] is None and row["clean"]
    assert str(await pool.fetchval("SELECT embedding_config_id FROM knowledge_bases WHERE id = $1", other["id"])) == str(other["embedding_config_id"])


# --- tenant isolation ----------------------------------------------------------------------------------------------

@pytest_asyncio.fixture(loop_scope="session")
async def app_pool():
    dsn = os.environ["POSTGRES_DSN"]
    try:
        p = await asyncpg.create_pool(f"postgresql://yuviz_app:testpw@localhost:5432/{dsn.rsplit('/', 1)[1]}", min_size=1, max_size=4)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"yuviz_app role not available: {exc}")
    yield p
    await p.close()


async def test_another_tenants_config_or_kb_is_invisible_to_the_migration(app_pool, pool, mig):
    other_tenant = dict(await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ('o', $1) RETURNING *", f"o-{uuid.uuid4().hex[:8]}"))
    try:
        foreign = await _config(other_tenant, "Embed-foreign", "m-x")
        set_caller_tenant(None)
        mgr = _manager({})
        with pytest.raises(ValueError):  # my KB, their config
            await reembed.reembed_kb(app_pool, mgr, tenant_id=str(mig["tenant"]["id"]), kb_id=mig["kb"]["id"], to_config=foreign["id"])
        with pytest.raises(ValueError):  # their scope, my KB
            await reembed.reembed_kb(app_pool, mgr, tenant_id=str(other_tenant["id"]), kb_id=mig["kb"]["id"], to_config=foreign["id"])
        with pytest.raises(ValueError):
            await reembed.abandon(app_pool, str(other_tenant["id"]), mig["kb"]["id"])
        report = await reembed.reembed_kb(app_pool, _manager(_spaces(mig)), tenant_id=str(mig["tenant"]["id"]), kb_id=mig["kb"]["id"], to_config=mig["b"])
        assert report.swapped  # the legitimate path works through the RLS-bound role
    finally:
        await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", other_tenant["id"])
        await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


# --- concurrent ingest ---------------------------------------------------------------------------------------------

_DOC = (b"Warranty claims for the Zephyr line are handled within 14 days. Replacement parts ship free of charge. "
        b"Contact the service desk with the serial number printed under the base of the unit. ") * 3


async def _ingest(m, manager, title="late upload"):
    set_caller_tenant(str(m["tenant"]["id"]))
    doc = await documents_service.upload_document(
        tenant_id=m["tenant"]["id"], kb_id=m["kb"]["id"], title=title, filename=f"{title}.txt",
        content_type="text/plain", content=_DOC, storage=LocalStorageProvider())
    set_caller_tenant(None)
    from services.knowledge.db import get_pool
    p = await get_pool()
    job = dict(await p.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    await process_one_job(p, LocalStorageProvider(), manager, job)
    return doc


async def test_a_document_ingested_mid_migration_is_not_lost_or_left_in_the_old_space(pool, mig):
    spaces = _spaces(mig)
    state = {"done": False}

    def after_first_batch(report):
        state["pending"] = True

    async def ingest_once():
        if not state["done"] and spaces[mig["b"]].calls >= 2:
            state["done"] = True
            state["doc"] = await _ingest(mig, _manager(spaces))  # uses the KB's CURRENT config: still A

    spaces[mig["b"]].hook = ingest_once
    report = await _run(pool, mig, "b", spaces, batch=2)
    assert state["done"] and report.swapped
    assert report.embedded > 7  # the late chunks were embedded too (swap refused once, then another pass)
    cfg, chunks = await _state(pool, mig)
    children = [c for c in chunks if not c["is_parent"] and c["emb"] is not None]
    assert cfg == mig["b"] and len(children) > 7
    assert all(_in_space(spaces[mig["b"]], c) and c["embedding_model"] == "m-b" for c in children)  # one vector space
    late = await pool.fetchval("SELECT count(*) FROM kb_chunks WHERE document_id = $1", state["doc"]["id"])
    assert late > 0


async def test_a_reingest_that_replaces_chunks_mid_migration_leaves_no_stale_shadow(pool, mig):
    spaces = _spaces(mig)
    doc_id = mig["docs"][1]["id"]  # Doc B: re-ingest replaces its chunks (new ids) after they were shadowed
    state = {"done": False}

    async def reingest():
        if not state["done"] and spaces[mig["b"]].calls >= 3:
            state["done"] = True
            await pool.execute("DELETE FROM kb_chunks WHERE document_id = $1", doc_id)
            for i, text in enumerate(["replacement chunk one about returns", "replacement chunk two about refunds"]):
                await pool.execute(
                    "INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content, embedding, embedding_model, version) "
                    "VALUES ($1, $2, $3, $4, $5, $6::vector, 'm-a', 2)", doc_id, mig["kb"]["id"], mig["tenant"]["id"],
                    i, text, vec_literal(mig["v1"].vec(text)))

    spaces[mig["b"]].hook = reingest
    report = await _run(pool, mig, "b", spaces, batch=2)
    assert state["done"] and report.swapped
    cfg, chunks = await _state(pool, mig)
    children = [c for c in chunks if not c["is_parent"]]
    assert cfg == mig["b"] and all(_in_space(spaces[mig["b"]], c) for c in children)
    assert any("replacement chunk" in c["content"] for c in children)


async def test_a_worker_that_embedded_with_the_old_config_cannot_land_after_the_swap(pool, mig):
    spaces = _spaces(mig)
    swapped = {"report": None}

    async def swap_during_embed():
        # The worker has fetched config A and is inside the embed call; the migration finishes right now.
        if swapped["report"] is None:
            swapped["report"] = "running"
            swapped["report"] = await _run(pool, mig, "b", spaces)

    spaces[mig["a"]].hook = swap_during_embed
    doc = await _ingest(mig, _manager(spaces), "racing upload")
    assert swapped["report"].swapped
    job = await pool.fetchrow("SELECT status, error_class FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"])
    assert job["status"] == "pending" and job["error_class"] == "transient"  # refused, scheduled for retry
    assert await pool.fetchval("SELECT count(*) FROM kb_chunks WHERE document_id = $1", doc["id"]) == 0  # no old-space vectors landed
    spaces[mig["a"]].hook = None
    await pool.execute("UPDATE kb_ingestion_jobs SET next_attempt_at = now() WHERE id = $1", doc["ingestion_job_id"])
    set_caller_tenant(None)
    from services.knowledge.db import get_pool
    p = await get_pool()
    job = dict(await p.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    await process_one_job(p, LocalStorageProvider(), _manager(spaces), job)  # retry: reads the KB's new config
    rows = await pool.fetch("SELECT content, embedding::text AS emb, embedding_model FROM kb_chunks WHERE document_id = $1 AND NOT is_parent", doc["id"])
    assert rows and all(_in_space(spaces[mig["b"]], r) and r["embedding_model"] == "m-b" for r in rows)


# --- the lock protocol between the worker and the swap -------------------------------------------------------------

async def test_the_swap_refuses_while_any_chunk_has_no_shadow_vector(pool, mig):
    await _run(pool, mig, "b", batch=2, max_batches=1)
    from services.knowledge.embedding_manager import EmbeddingProviderConfig
    cfg = EmbeddingProviderConfig(id=mig["b"], engine="ollama", model="m-b")
    assert await reembed.swap(pool, str(mig["tenant"]["id"]), mig["kb"]["id"], cfg) == (False, "incomplete")
    assert (await _state(pool, mig))[0] == mig["a"]


async def test_the_swap_waits_for_an_in_flight_worker_transaction_and_then_sees_its_chunks(pool, mig):
    from services.knowledge.embedding_manager import EmbeddingProviderConfig
    spaces = _spaces(mig)
    # fill the shadow completely without swapping: stop right before the swap by asking for exactly the batches needed
    await _run(pool, mig, "b", spaces, batch=100, max_batches=1)
    cfg = EmbeddingProviderConfig(id=mig["b"], engine="ollama", model="m-b")
    tenant_id = str(mig["tenant"]["id"])
    async with pool.acquire() as worker:
        tx = worker.transaction()
        await tx.start()
        await reembed.assert_kb_config_current(worker, mig["kb"]["id"], mig["a"], mig["tenant"]["id"])  # what the worker's index step does
        swap_task = asyncio.create_task(reembed.swap(pool, tenant_id, mig["kb"]["id"], cfg))
        await asyncio.sleep(0.3)
        assert not swap_task.done(), "the swap did not wait for the worker transaction holding the KB row"
        await worker.execute(  # the worker's chunk, embedded with the OLD config, lands before the swap can look
            "INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content, embedding, embedding_model) "
            "VALUES ($1, $2, $3, 99, 'late chunk', $4::vector, 'm-a')", mig["docs"][0]["id"], mig["kb"]["id"],
            mig["tenant"]["id"], vec_literal(mig["v1"].vec("late chunk")))
        await tx.commit()
    assert await asyncio.wait_for(swap_task, 5) == (False, "incomplete")  # saw the committed chunk, refused
    assert (await _state(pool, mig))[0] == mig["a"]


# --- ownership, enforced by the module (the CLI runs on the service DSN, where RLS does not apply) -------------------

@pytest_asyncio.fixture(loop_scope="session")
async def foreign(pool, mig):
    """A second tenant with its own KB, chunks and embedding config, all reachable by id on the non-RLS pool."""
    other = dict(await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ('o', $1) RETURNING *", f"o-{uuid.uuid4().hex[:8]}"))
    agent = dict(await pool.fetchrow("INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'x', 'X') RETURNING *", other["id"]))
    set_caller_tenant(str(other["id"]))
    kb = await make_kb(pool, other, agent, "theirs")
    await add_document(pool, other, kb, "Their doc", ["their confidential chunk one", "their confidential chunk two"])
    cfg = await _config(other, "Embed-theirs", "m-x")
    set_caller_tenant(None)
    yield {"tenant": other, "kb": kb, "cfg": cfg}
    await pool.execute("DELETE FROM audit_log WHERE tenant_id = $1", other["id"])
    await pool.execute("DELETE FROM kb_chunks WHERE tenant_id = $1", other["id"])
    await pool.execute("DELETE FROM kb_documents WHERE tenant_id = $1", other["id"])
    await pool.execute("DELETE FROM agent_knowledge_bases WHERE agent_id = $1", agent["id"])
    await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", other["id"])
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", other["id"])
    await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", other["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])


async def _snapshot(pool, *tenant_ids):
    chunks = await pool.fetch(
        "SELECT id, embedding::text, embedding_model, embedding_next::text, embedding_next_model FROM kb_chunks "
        "WHERE tenant_id = ANY($1) ORDER BY id", list(tenant_ids))
    kbs = await pool.fetch("SELECT id, embedding_config_id, config_version FROM knowledge_bases WHERE tenant_id = ANY($1) ORDER BY id", list(tenant_ids))
    audits = await pool.fetchval("SELECT count(*) FROM audit_log WHERE tenant_id = ANY($1)", list(tenant_ids))
    return [tuple(r) for r in chunks], [tuple(r) for r in kbs], audits


def _spied(mig, foreign):
    spaces = {**_spaces(mig), str(foreign["cfg"]["id"]): Space("theirs")}
    return spaces


async def _refused(pool, mig, foreign, spaces, **call):
    """Run a cross-tenant call on the service pool: it must raise ValueError, call no provider and write nothing."""
    ids = [mig["tenant"]["id"], foreign["tenant"]["id"]]
    before = await _snapshot(pool, *ids)
    set_caller_tenant(None)
    with pytest.raises(ValueError):
        await reembed.reembed_kb(pool, _manager(spaces), **call)
    assert all(sp.calls == 0 for sp in spaces.values()), "a provider was called for a refused migration"
    assert await _snapshot(pool, *ids) == before


async def test_a_config_of_another_tenant_is_refused_before_any_text_leaves(pool, mig, foreign):
    spaces = _spaces(mig) | {str(foreign["cfg"]["id"]): Space("theirs")}
    for dry in (False, True):
        await _refused(pool, mig, foreign, spaces, tenant_id=str(mig["tenant"]["id"]), kb_id=mig["kb"]["id"],
                       to_config=foreign["cfg"]["id"], dry_run=dry)


async def test_a_kb_of_another_tenant_is_refused(pool, mig, foreign):
    spaces = _spaces(mig) | {str(foreign["cfg"]["id"]): Space("theirs")}
    for dry in (False, True):
        await _refused(pool, mig, foreign, spaces, tenant_id=str(mig["tenant"]["id"]), kb_id=foreign["kb"]["id"],
                       to_config=mig["b"], dry_run=dry)


async def test_the_wrong_tenant_flag_is_refused(pool, mig, foreign):
    spaces = _spaces(mig) | {str(foreign["cfg"]["id"]): Space("theirs")}
    for dry in (False, True):  # their tenant id with my KB and my config
        await _refused(pool, mig, foreign, spaces, tenant_id=str(foreign["tenant"]["id"]), kb_id=mig["kb"]["id"],
                       to_config=mig["b"], dry_run=dry)
    await _refused(pool, mig, foreign, spaces, tenant_id=str(foreign["tenant"]["id"]), kb_id=mig["kb"]["id"],
                   to_config=foreign["cfg"]["id"])


async def test_abandon_under_the_wrong_tenant_clears_nothing(pool, mig, foreign):
    await _run(pool, mig, "b", batch=2, max_batches=1)
    before = await _snapshot(pool, mig["tenant"]["id"], foreign["tenant"]["id"])
    with pytest.raises(ValueError):
        await reembed.abandon(pool, str(foreign["tenant"]["id"]), mig["kb"]["id"])
    assert await _snapshot(pool, mig["tenant"]["id"], foreign["tenant"]["id"]) == before
    assert await reembed.abandon(pool, str(mig["tenant"]["id"]), mig["kb"]["id"]) == 2  # the owner can


async def test_bad_ids_are_refused_not_executed(pool, mig):
    set_caller_tenant(None)
    for kw in ({"tenant_id": "x' OR '1'='1"}, {"kb_id": "not-a-uuid"}, {"to_config": "nope"}):
        args = {"tenant_id": str(mig["tenant"]["id"]), "kb_id": mig["kb"]["id"], "to_config": mig["b"]} | kw
        with pytest.raises(ValueError):
            await reembed.reembed_kb(pool, _manager(_spaces(mig)), **args)


async def test_the_swap_and_audit_are_recorded_under_the_kbs_owner(pool, mig, foreign):
    assert (await _run(pool, mig, "b")).swapped
    owners = {r["tenant_id"] for r in await pool.fetch("SELECT tenant_id FROM audit_log WHERE entity_id = $1 AND entity_type = 'knowledge_base' AND new_value->>'embedding_config_id' = $2", mig["kb"]["id"], mig["b"])}
    assert owners == {mig["tenant"]["id"]}
    theirs = await pool.fetch("SELECT embedding_next IS NULL AS clean, embedding_model FROM kb_chunks WHERE tenant_id = $1", foreign["tenant"]["id"])
    assert theirs and all(r["clean"] and r["embedding_model"] is None for r in theirs)


# --- soft-deleted documents ----------------------------------------------------------------------------------------

async def test_soft_deleted_documents_are_not_sent_out_again_and_leave_no_old_space_vectors(pool, mig):
    gone = await add_document(pool, mig["tenant"], mig["kb"], "Deleted doc", ["deleted secret text one", "deleted secret text two"], deleted=True)
    for cid in gone["chunk_ids"]:
        await pool.execute("UPDATE kb_chunks SET embedding_model = 'm-a' WHERE id = $1", cid)
    v2 = Space("v2")
    sent = []
    orig = v2.embed

    async def spy(texts):
        sent.extend(texts)
        return await orig(texts)
    v2.embed = spy
    report = await _run(pool, mig, "b", _spaces(mig, v2))
    assert report.swapped and report.embedded == 7 and not any("deleted secret" in t for t in sent)
    rows = await pool.fetch("SELECT embedding IS NULL AS none, embedding_next IS NULL AS clean FROM kb_chunks WHERE document_id = $1", gone["id"])
    assert rows and all(r["none"] and r["clean"] for r in rows)  # no vector left in the wrong space


# --- the swap's lock and uploads -------------------------------------------------------------------------------------

async def test_uploads_proceed_while_the_real_swap_holds_the_kb_row(pool, mig, monkeypatch):
    await _run(pool, mig, "b", batch=100, max_batches=1)
    inside = asyncio.Event()
    orig = audit.write_audit

    async def slow_audit(*a, **k):  # runs inside the swap transaction, after it took the KB row lock
        inside.set()
        await asyncio.sleep(1.0)
        return await orig(*a, **k)
    monkeypatch.setattr(audit, "write_audit", slow_audit)
    swap_task = asyncio.create_task(_run(pool, mig, "b"))
    await asyncio.wait_for(inside.wait(), 5)
    t0 = asyncio.get_running_loop().time()
    await asyncio.wait_for(add_document(pool, mig["tenant"], mig["kb"], "uploaded during swap", ["x"], embed=False), 0.6)
    assert asyncio.get_running_loop().time() - t0 < 0.6 and not swap_task.done()
    assert (await swap_task).swapped


async def test_the_worker_guard_is_tenant_scoped(pool, mig, foreign):
    async with pool.acquire() as conn:
        with pytest.raises(reembed.KbConfigChanged):
            await reembed.assert_kb_config_current(conn, mig["kb"]["id"], mig["a"], foreign["tenant"]["id"])
        await reembed.assert_kb_config_current(conn, mig["kb"]["id"], mig["a"], mig["tenant"]["id"])


async def test_a_swap_over_the_threshold_needs_allow_long_swap(pool, mig, monkeypatch):
    monkeypatch.setattr(reembed, "MAX_SWAP_CHUNKS", 5)
    v2 = Space("v2")
    with pytest.raises(ValueError, match="allow-long-swap"):
        await _run(pool, mig, "b", _spaces(mig, v2))
    assert v2.calls == 0  # refused before spending any embedding calls
    dry = await _run(pool, mig, "b", dry_run=True)
    assert dry.too_large and dry.swap_chunks == 7
    assert (await _run(pool, mig, "b", allow_long_swap=True)).swapped


async def test_a_worker_waiting_on_the_swap_keeps_a_valid_lease_and_is_not_reaped_or_duplicated(pool, mig):
    """The reviewer's worry: a worker blocked on the swap's lock outlives the 120 s lease and is reaped. The reaper
    skips job rows that are locked (the worker holds its own job row FOR UPDATE while it waits), so it cannot take
    the job; once the swap commits the worker's guard refuses its old-space vectors and the retry succeeds."""
    from services.knowledge.ingestion_worker import reap_expired_leases
    spaces = _spaces(mig)
    await _run(pool, mig, "b", spaces, batch=100, max_batches=1)
    from services.knowledge.embedding_manager import EmbeddingProviderConfig
    cfg = EmbeddingProviderConfig(id=mig["b"], engine="ollama", model="m-b")
    from services.knowledge.db import get_pool
    p = await get_pool()
    set_caller_tenant(str(mig["tenant"]["id"]))
    doc = await documents_service.upload_document(
        tenant_id=mig["tenant"]["id"], kb_id=mig["kb"]["id"], title="slow", filename="s.txt", content_type="text/plain",
        content=_DOC, storage=LocalStorageProvider())
    set_caller_tenant(None)
    job = dict(await p.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    async with pool.acquire() as swapper:
        tx = swapper.transaction()
        await tx.start()
        await swapper.fetchval("SELECT 1 FROM knowledge_bases WHERE id = $1 FOR NO KEY UPDATE", mig["kb"]["id"])  # the swap, mid-flight
        worker = asyncio.create_task(process_one_job(p, LocalStorageProvider(), _manager(spaces), job, lease_s=1.0, heartbeat_s=0.2))
        await asyncio.sleep(2.5)  # past the 1 s lease
        assert not worker.done()  # blocked on the KB row
        assert await reap_expired_leases(p) == 0  # the locked job row is skipped, not reaped
        await tx.rollback()
    await asyncio.wait_for(worker, 10)
    row = await pool.fetchrow("SELECT status, attempts FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"])
    assert row["status"] == "succeeded" and row["attempts"] == 1


# --- throttle ---------------------------------------------------------------------------------------------------------

async def test_batches_are_paced(pool, mig, monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        await real_sleep(0)
    monkeypatch.setattr(reembed.asyncio, "sleep", fake_sleep)
    report = await _run(pool, mig, "b", batch=2, sleep_s=0.5)
    assert report.swapped and len([s for s in sleeps if s > 0.4]) == report.batches == 4
    sleeps.clear()
    from services.knowledge.db import get_pool  # noqa: F401
    await _run(pool, mig, "c", batch=3, max_batches_per_minute=30)
    assert len([s for s in sleeps if 1.5 < s <= 2.0]) == 3  # 60/30 = 2 s per batch


_DOC = (b"Warranty claims for the Zephyr line are handled within 14 days. Replacement parts ship free of charge. "
        b"Contact the service desk with the serial number printed under the base of the unit. ") * 3


# --- a stale plan after the swap --------------------------------------------------------------------------------------

async def _stale_plan_after_swap(pool, mig, spaces):
    """The residual window: another process did the swap, and this process still holds the plan from before it."""
    from services.knowledge import cache
    set_caller_tenant(str(mig["tenant"]["id"]))
    warm = await retrieval.retrieve(None, PgVectorRepository(), _manager(spaces), tenant_slug=mig["tenant"]["slug"],
                                    agent_slug="sup", query=TEXTS[1], minimum_score=0.3)
    assert warm["status"] == "ok"
    key = cache.plan_key(mig["tenant"]["slug"], "sup")
    stale = retrieval_plan_cache().get(key)
    assert stale is not None and mig["a"] in stale.groups
    assert (await _run(pool, mig, "b", spaces)).swapped
    retrieval_plan_cache().set(key, stale)  # the swap invalidated this process's cache; put the old plan back
    return stale


def retrieval_plan_cache():
    from services.knowledge import retrieval_plan
    return retrieval_plan._plans


async def test_a_stale_plan_never_searches_new_vectors_with_an_old_model_query_and_says_so(pool, mig):
    spaces = _spaces(mig)
    await _stale_plan_after_swap(pool, mig, spaces)
    set_caller_tenant(str(mig["tenant"]["id"]))
    for hybrid in (False, True):
        r = await retrieval.retrieve(None, PgVectorRepository(), _manager(spaces), tenant_slug=mig["tenant"]["slug"],
                                     agent_slug="sup", query=TEXTS[1], minimum_score=0.3, hybrid_search=hybrid)
        assert "embedding_model_mismatch" in r["degraded"]          # visible, not an ordinary ok
        assert r["retrieval_metadata"]["dense_candidates"] == 0     # no old-model query met a new-model vector
        if hybrid:
            assert r["status"] == "ok" and any(TEXTS[1] in c["content"] for c in r["chunks"])  # lexical still answers


async def test_a_new_plan_after_the_swap_has_no_mismatch_and_a_genuine_no_match_is_not_flagged(pool, mig):
    spaces = _spaces(mig)
    assert (await _run(pool, mig, "b", spaces)).swapped
    retrieval.clear_caches()
    set_caller_tenant(str(mig["tenant"]["id"]))
    ok = await retrieval.retrieve(None, PgVectorRepository(), _manager(spaces), tenant_slug=mig["tenant"]["slug"],
                                  agent_slug="sup", query=TEXTS[2], minimum_score=0.3)
    assert ok["status"] == "ok" and "embedding_model_mismatch" not in ok["degraded"]
    nothing = await retrieval.retrieve(None, PgVectorRepository(), _manager(spaces), tenant_slug=mig["tenant"]["slug"],
                                       agent_slug="sup", query="completely unrelated zebra quantum", minimum_score=0.3)
    assert "embedding_model_mismatch" not in nothing["degraded"]


async def test_chunks_without_a_recorded_model_stay_searchable(pool, mig):
    await pool.execute("UPDATE kb_chunks SET embedding_model = NULL WHERE kb_id = $1", mig["kb"]["id"])
    set_caller_tenant(str(mig["tenant"]["id"]))
    r = await retrieval.retrieve(None, PgVectorRepository(), _manager(_spaces(mig)), tenant_slug=mig["tenant"]["slug"],
                                 agent_slug="sup", query=TEXTS[3], minimum_score=0.3, hybrid_search=False)
    assert r["status"] == "ok" and "embedding_model_mismatch" not in r["degraded"]
