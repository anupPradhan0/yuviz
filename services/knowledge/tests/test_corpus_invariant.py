"""Chaos corpus through the real worker: every file ends in ready / ready_degraded / failed with a stored reason,
none stays `processing`, none raises out of process_one_job. Needs Postgres, not Ollama (fake embedder)."""

from __future__ import annotations

import pytest

from services.knowledge import documents as documents_service
from services.knowledge.ingestion_worker import process_one_job
from services.knowledge.storage import LocalStorageProvider
from services.knowledge.tests.corpus.builders import build_corpus
from services.knowledge.tests.test_ingestion_queue import _FakeProvider, _make_kb, _manager, _upload, _purge_tenant

CORPUS = build_corpus()


async def _run(pool, tenant, kb, name: str):
    f = CORPUS[name]
    doc = await _upload(tenant, kb, name.replace(".", "-"), content=f.data, content_type=f.declared_type)
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    await process_one_job(pool, LocalStorageProvider(), _manager(_FakeProvider()), job)
    row = dict(await pool.fetchrow("SELECT * FROM kb_documents WHERE id = $1", doc["id"]))
    job_row = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", job["id"]))
    return row, job_row


@pytest.fixture
def small_bomb_limit(monkeypatch):
    monkeypatch.setenv("KB_PARSE_MAX_DECOMPRESSED", str(2 * 1024 * 1024))
    # 5 MB of prose is ~4,300 fixed windows or ~8,300 structure chunks; the shipped ceiling is 5,000 per document
    # (KNOWLEDGE_MAX_CHUNKS_PER_DOC), so the corpus run raises it to keep "5 MB of text is ready" meaningful.
    monkeypatch.setenv("KNOWLEDGE_MAX_CHUNKS_PER_DOC", "20000")


async def test_every_corpus_file_ends_in_a_defined_state(tenant_agent, pool, small_bomb_limit, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # no OCR engine: scanned pages must degrade, not hang
    tenant, _ = tenant_agent
    kb = await _make_kb(tenant, "chaos-kb")
    results = {}
    for name, f in CORPUS.items():
        row, job = await _run(pool, tenant, kb, name)  # a raise here is the failure the invariant forbids
        results[name] = (row["status"], row["error"], job["status"])
        assert row["status"] != "processing", name
        assert row["status"] in f.allowed, (name, results[name])
        if row["status"] == "failed":
            assert row["error"] and job["status"] == "failed", (name, results[name])
        else:
            assert job["status"] == "succeeded", name
            assert await pool.fetchval("SELECT count(*) FROM kb_chunks WHERE document_id = $1", row["id"]) > 0, name
        if row["status"] == "ready_degraded":
            assert row["error"], name  # the reason a part was unreadable is kept
    await _purge_tenant(pool, tenant["id"])

    assert results["scanned.pdf"][1] == "scanned PDF needs OCR; no OCR engine available"
    assert results["encrypted.pdf"][1] == "document is password-protected"
    assert results["empty.txt"][1] == "file is empty"
    assert "decompression bomb" in results["zip_bomb.docx"][1]
    assert results["half_scanned.pdf"][0] == "ready_degraded"


async def test_parser_metadata_is_stored_and_prompt_injection_is_just_content(tenant_agent, pool):
    tenant, _ = tenant_agent
    kb = await _make_kb(tenant, "meta-kb")
    row, _ = await _run(pool, tenant, kb, "tables.docx")
    assert row["status"] == "ready" and row["parser_used"] == "python-docx" and row["quality_score"] == 1.0

    row, _ = await _run(pool, tenant, kb, "injection.md")
    assert row["status"] == "ready"  # content is data: indexed verbatim, never acted on at ingestion
    chunks = await pool.fetch("SELECT content FROM kb_chunks WHERE document_id = $1", row["id"])
    assert any("Ignore previous instructions" in c["content"] for c in chunks)
    await _purge_tenant(pool, tenant["id"])


async def test_upload_route_decides_by_bytes_not_declared_type(test_tenant, test_admin, pool, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from libs.tenancy import set_caller_tenant
    from services.knowledge import knowledge_bases as kb_service
    from services.knowledge.app import app

    set_caller_tenant(str(test_tenant["id"]))
    kb = await kb_service.create_knowledge_base(tenant_id=test_tenant["id"], slug="route-kb", name="Route")
    headers = {"Authorization": f"Bearer {test_admin['token']}"}

    async def post(name: str, data: bytes, declared: str):
        return await c.post(f"/knowledge-bases/{kb['id']}/documents", files={"file": (name, data, declared)},
                            data={"title": name}, headers=headers)

    import threading
    from services.knowledge.routers import documents as documents_router

    detect_threads: list[threading.Thread] = []
    real_detect = documents_router.detect_content_type

    def spy(*a, **k):
        detect_threads.append(threading.current_thread())
        return real_detect(*a, **k)

    monkeypatch.setattr(documents_router, "detect_content_type", spy)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        pdf = await post("policy.txt", CORPUS["text.pdf"].data, "text/plain")  # a PDF mislabelled as text
        assert pdf.status_code == 201 and pdf.json()["content_type"] == "application/pdf"
        docx = await post("blob.bin", CORPUS["tables.docx"].data, "application/octet-stream")
        assert docx.status_code == 201 and docx.json()["content_type"].endswith("wordprocessingml.document")
        png = await post("notes.txt", CORPUS["fake_extension_png.txt"].data, "text/plain")  # an image called .txt
        assert png.status_code == 415 and "not supported" in png.json()["detail"]
        assert (await post("empty.txt", b"", "text/plain")).status_code == 400
    # detection is CPU work (zip inspection, charset detection): it must not run on the API event-loop thread
    assert detect_threads and all(t is not threading.main_thread() for t in detect_threads)
    await pool.execute("DELETE FROM kb_ingestion_jobs WHERE kb_id = $1", kb["id"])
    await pool.execute("DELETE FROM audit_log WHERE tenant_id = $1", test_tenant["id"])
    await pool.execute("DELETE FROM kb_documents WHERE kb_id = $1", kb["id"])
