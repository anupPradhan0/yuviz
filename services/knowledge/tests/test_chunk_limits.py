"""Chunking is linear, runs off the event loop, and is bounded by a wall-clock budget and a per-document chunk ceiling,
so no uploaded file can stall the worker, flood the embedding provider or exhaust memory (review + security findings)."""

from __future__ import annotations

import asyncio
import time
from array import array

import pytest

from services.knowledge.chunker import chunk_document
from services.knowledge.chunking import ChunkLimitError, chunk_text
from services.knowledge.embedding_manager import embed_in_batches
from services.knowledge.ingestion_worker import classify_error, process_one_job
from services.knowledge.parsers import parse_document
from services.knowledge.parsers.model import TABLE, Block
from services.knowledge.parsers.textblocks import blocks_from_text
from services.knowledge.storage import LocalStorageProvider
from services.knowledge.tests.test_ingestion_queue import _FakeProvider, _make_kb, _manager, _purge_tenant, _upload

CJK_10MB = ("退款政策客户可以在购买之日起三十天内申请全额退款" * 400_000)           # ~10 MB of UTF-8, one line
LATIN_10MB = "refund " * 1_430_000                                              # ~10 MB, one line


@pytest.fixture(autouse=True)
def _auto(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_CHUNKER", "auto")


async def _timed_in_thread(blocks, **kw):
    """Chunk off the loop while a heartbeat-like task ticks every 10 ms; returns (result|exception, seconds, ticks)."""
    ticks = 0

    async def beat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(beat())
    t0 = time.monotonic()
    try:
        out = await asyncio.to_thread(chunk_document, blocks, **kw)
    except ChunkLimitError as exc:
        out = exc
    finally:
        task.cancel()
    return out, time.monotonic() - t0, ticks


@pytest.mark.parametrize("name,text", [("cjk", CJK_10MB), ("latin", LATIN_10MB)])
async def test_ten_megabytes_without_a_blank_line_chunks_in_seconds_and_the_loop_stays_responsive(name, text, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_MAX_CHUNKS_PER_DOC", "1000000")
    result, seconds, ticks = await _timed_in_thread(blocks_from_text(text))
    assert not isinstance(result, Exception) and len(result.children) > 5000
    assert seconds < 20, f"{name}: chunking took {seconds:.1f}s (was 151 s when quadratic)"
    assert ticks >= 20, f"{name}: the event loop only ticked {ticks} times while chunking ran"


def test_the_windowing_is_linear_not_quadratic():
    small = CJK_10MB[: len(CJK_10MB) // 10]
    t0 = time.monotonic()
    chunk_text(small)
    t_small = time.monotonic() - t0
    t0 = time.monotonic()
    chunk_text(CJK_10MB)
    t_big = time.monotonic() - t0
    assert t_big < max(0.05, t_small) * 25   # 10x the input: ~10x the time; the quadratic version was ~100x


async def test_the_default_ceiling_rejects_before_producing_millions_of_chunks():
    result, seconds, _ = await _timed_in_thread(blocks_from_text(CJK_10MB))
    assert isinstance(result, ChunkLimitError) and "more than 5000 chunks" in str(result) and seconds < 15
    assert classify_error(result) == "permanent"


async def test_the_wall_clock_budget_is_cooperative_and_final(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_CHUNK_BUDGET_S", "0.05")
    monkeypatch.setenv("KNOWLEDGE_MAX_CHUNKS_PER_DOC", "10000000")
    result, seconds, _ = await _timed_in_thread(blocks_from_text(LATIN_10MB))
    assert isinstance(result, ChunkLimitError) and "too complex to index" in str(result) and seconds < 10


@pytest.mark.parametrize("marks,ceiling", [(100_000, 1000), (400_000, 3000)])
def test_combining_marks_are_not_one_chunk_per_character(marks, ceiling):
    r = chunk_document(blocks_from_text("Policy a" + "́" * marks))
    assert 0 < len(r.children) < ceiling, len(r.children)     # was 49,602 for 100 KB and 199,603 for 400 KB
    assert all(len(c.content) >= 20 for c in r.children[1:-1])  # no tiny pieces (the first is the word "Policy", the last the remainder)


async def test_a_file_of_one_a_and_a_million_accents_is_bounded():
    raw = ("a" * 950_000 + "́" * 1_000_000).encode()
    parsed = await parse_document(raw)
    result, seconds, _ = await _timed_in_thread(parsed.blocks)
    assert seconds < 15
    assert isinstance(result, ChunkLimitError) or len(result.children) <= 5000   # was 999,603 chunks / 49 s


async def test_the_wide_csv_ends_in_a_clear_permanent_rejection_quickly():
    rows = tuple(tuple("x" for _ in range(1999)) for _ in range(1000))   # 3.9 MB: chunked for 196 s before
    result, seconds, _ = await _timed_in_thread([Block(TABLE, rows=rows)])
    assert isinstance(result, ChunkLimitError) and "5000 chunks" in str(result) and seconds < 15


async def test_the_worker_rejects_an_over_ceiling_document_before_embedding(tenant_agent, pool):
    tenant, _ = tenant_agent
    kb = await _make_kb(tenant, "limits-kb")
    doc = await _upload(tenant, kb, "flood", content=("a" * 20_000 + "́" * 3_000_000).encode(), content_type="text/plain")
    provider = _FakeProvider()
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    import os
    os.environ["KNOWLEDGE_MAX_CHUNKS_PER_DOC"] = "50"
    try:
        await process_one_job(pool, LocalStorageProvider(), _manager(provider), job)
    finally:
        del os.environ["KNOWLEDGE_MAX_CHUNKS_PER_DOC"]
    d = dict(await pool.fetchrow("SELECT * FROM kb_documents WHERE id = $1", doc["id"]))
    j = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", job["id"]))
    assert d["status"] == "failed" and "too large to index (more than 50 chunks)" in d["error"]
    assert j["status"] == "failed" and j["error_class"] == "permanent" and j["attempts"] == 1
    assert provider.calls == 0   # nothing was sent to the embedding provider
    await _purge_tenant(pool, tenant["id"])


async def test_vectors_are_held_as_compact_float32_arrays():
    class P:
        async def embed(self, texts):
            return [[0.5] * 768 for _ in texts]

    vectors = await embed_in_batches(P(), ["a"] * 100)
    assert all(isinstance(v, array) and v.itemsize == 4 and len(v) == 768 for v in vectors)


async def test_the_worker_keeps_its_event_loop_responsive_while_it_chunks(tenant_agent, pool, monkeypatch):
    """The heartbeat shares the loop with chunking, so a stall longer than the lease would get the job reaped."""
    monkeypatch.setenv("KNOWLEDGE_MAX_CHUNKS_PER_DOC", "1000000")
    monkeypatch.setenv("KNOWLEDGE_CHUNKER", "structure")   # the slower route: ~6 s of CPU for this document
    tenant, _ = tenant_agent
    kb = await _make_kb(tenant, "loop-kb")
    doc = await _upload(tenant, kb, "big", content=("refund policy " * 500_000).encode(), content_type="text/plain")
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    gaps, last, stop = [], time.monotonic(), False

    async def beat():
        nonlocal last
        while not stop:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(beat())
    await process_one_job(pool, LocalStorageProvider(), _manager(_FakeProvider()), job)
    stop = True
    await task
    assert max(gaps) < 0.5, f"the event loop stalled for {max(gaps):.2f}s while the worker chunked"
    await _purge_tenant(pool, tenant["id"])
