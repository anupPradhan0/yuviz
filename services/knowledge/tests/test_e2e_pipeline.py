"""End to end: upload through the real knowledge FastAPI app (httpx ASGI transport), ingest with the real worker
functions, retrieve through POST /internal/retrieve, and read the outcome back through the public routes.
Real Postgres; the only fake is the embedding model (a deterministic bag-of-words embedder, see rag_fakes.py), so
no Ollama is needed. Each test builds its own tenant(s), so it does not depend on another test's rows."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from libs.tenancy import set_caller_tenant, set_target_tenant
from services.config import auth as config_auth
from services.config import users as users_service
from services.knowledge import db, ops_metrics, retrieval, retrieval_log
from services.knowledge import retrieval_plan as plans
from services.knowledge.app import app
from services.knowledge.ingestion_worker import _claim_next_job, process_one_job
from services.knowledge.resilience import RetrievalUnavailable, admission
from services.knowledge.routers import retrieve as retrieve_router
from services.knowledge.runtime import get_vector_repo
from services.knowledge.storage import LocalStorageProvider
from services.knowledge.tests.corpus import builders
from services.knowledge.tests.rag_fakes import FakeEmbedder, make_kb, make_manager
from services.knowledge.tests.test_ingestion_queue import _drain_pending

PDF_TYPE = "application/pdf"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

MARKDOWN = (
    "# Handbook\n\n## Escalations\n\n"
    + "\n\n".join(f"Paragraph {i} covers topic{i}x. " + "Filler words repeat here and there. " * 12 for i in range(6))
    + "\n\n## Billing\n\nInvoices are sent monthly. " + "Billing text goes on and on. " * 30
).encode()


class DownEmbedder(FakeEmbedder):
    """Fails like an HTTP provider whose error text embeds the request URL with credentials."""

    async def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("embedding provider down at http://svc:hunter2@embed.internal:11434/api?key=sk-secret")
        return await super().embed(texts)


@dataclasses.dataclass
class Tenant:
    row: dict
    agent: dict
    kb: dict
    admin_token: str
    viewer_token: str

    @property
    def slug(self) -> str:
        return self.row["slug"]


@dataclasses.dataclass
class World:
    a: Tenant
    service_token: str
    client: AsyncClient
    embedder: DownEmbedder
    pool: Any

    # -- helpers ------------------------------------------------------------------------------------------

    def auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    async def upload(self, name: str, data: bytes, ctype: str, *, tenant: Tenant | None = None, title: str | None = None):
        t = tenant or self.a
        resp = await self.client.post(
            f"/knowledge-bases/{t.kb['id']}/documents", files={"file": (name, data, ctype)},
            data={"title": title or name}, headers=self.auth(t.admin_token),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def ingest(self, doc: dict, embedder: FakeEmbedder | None = None, tenant: Tenant | None = None) -> dict:
        """Run the worker on this document's job. Scope is cleared first: the ASGI transport runs the app in this
        task, so a previous request's caller tenant would otherwise leak into the worker's explicit tenant."""
        _clear_scope()
        job = dict(await self.pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
        await process_one_job(self.pool, LocalStorageProvider(), make_manager(embedder or self.embedder), job)
        return await self.document(doc["id"], (tenant or self.a).admin_token)

    async def document(self, document_id, token: str | None = None) -> dict:
        resp = await self.client.get(f"/documents/{document_id}", headers=self.auth(token or self.a.admin_token))
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def retrieve(self, query: str, *, tenant: Tenant | None = None, token: str | None = None,
                       tenant_slug: str | None = None, **extra) -> dict:
        t = tenant or self.a
        resp = await self.client.post(
            "/internal/retrieve",
            json={"tenant_slug": tenant_slug or t.slug, "agent_slug": t.agent["slug"], "query": query, **extra},
            headers=self.auth(token or self.service_token),
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def make_tenant(self, label: str, *, agent_slug: str = "sup", kb_slug: str = "kb") -> Tenant:
        slug = f"e2e-{label}-{uuid.uuid4().hex[:8]}"
        row = dict(await self.pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $1) RETURNING *", slug))
        agent = dict(await self.pool.fetchrow(
            "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, $2, 'Support') RETURNING *", row["id"], agent_slug))
        set_caller_tenant(str(row["id"]))
        kb = await make_kb(self.pool, row, agent, kb_slug)
        _clear_scope()
        tokens = {}
        for role in ("admin", "viewer"):
            user = await users_service.create_user(
                email=f"e2e-{role}-{uuid.uuid4().hex[:8]}@example.com", password="test-password-not-real",
                role=role, tenant_id=row["id"])
            tokens[role] = config_auth.create_access_token(user)
        return Tenant(row, agent, kb, tokens["admin"], tokens["viewer"])


def _clear_scope() -> None:
    set_caller_tenant(None)
    set_target_tenant(None)


async def _purge(pool, tenant_id) -> None:
    await retrieval_log.flush()
    for sql in (
        "DELETE FROM retrieval_logs WHERE tenant_id = $1",
        "DELETE FROM kb_chunks WHERE tenant_id = $1",
        "DELETE FROM kb_ingestion_events WHERE tenant_id = $1",
        "DELETE FROM kb_ingestion_jobs WHERE document_id IN (SELECT id FROM kb_documents WHERE tenant_id = $1)",
        "DELETE FROM audit_log WHERE tenant_id = $1",
        "DELETE FROM kb_documents WHERE tenant_id = $1",
        "DELETE FROM agent_knowledge_bases WHERE agent_id IN (SELECT id FROM agents WHERE tenant_id = $1)",
        "DELETE FROM knowledge_bases WHERE tenant_id = $1",
        "DELETE FROM agents WHERE tenant_id = $1",
        "DELETE FROM provider_configs WHERE tenant_id = $1",
        "UPDATE users SET deleted_at = now(), tenant_id = NULL WHERE tenant_id = $1",
        "DELETE FROM tenants WHERE id = $1",
    ):
        await pool.execute(sql, tenant_id)


@pytest_asyncio.fixture(loop_scope="session")
async def world(pool, test_service_viewer, monkeypatch, tmp_path):
    embedder = DownEmbedder()
    # The router resolves its manager through this name; ingestion gets its manager passed in.
    monkeypatch.setattr(retrieve_router, "get_embedding_manager", lambda: make_manager(embedder))
    monkeypatch.setenv("PATH", str(tmp_path))  # no OCR engine on PATH: a scanned PDF must fail visibly, not hang
    retrieval.clear_caches()
    ops_metrics.reset()
    created: list[Any] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        w = World(None, test_service_viewer["token"], client, embedder, pool)  # type: ignore[arg-type]
        w.a = await w.make_tenant("a")
        created.append(w.a.row["id"])
        real_make = w.make_tenant

        async def tracked(*a, **kw):
            t = await real_make(*a, **kw)
            created.append(t.row["id"])
            return t
        w.make_tenant = tracked  # type: ignore[method-assign]
        yield w
    _clear_scope()
    for tenant_id in created:
        await _purge(pool, tenant_id)


def _texts(result: dict) -> str:
    return "\n".join(c["content"] for c in result["chunks"])


# ── happy path, one test per document type ──────────────────────────────────────────────────────────────

async def test_pdf_upload_to_cited_answer(world):
    doc = await world.ingest(await world.upload("policy.pdf", builders.text_pdf(), PDF_TYPE, title="Refund PDF"))
    assert doc["status"] == "ready" and doc["content_type"] == PDF_TYPE

    result = await world.retrieve("refund within 30 days original payment method")
    assert result["status"] == "ok"
    assert "within 30 days" in _texts(result)
    assert result["sources"] == ["Refund PDF"] and result["include_citations"] is True
    source = result["chunks"][0]["source"]
    assert source["document_id"] == str(doc["id"]) and source["document_title"] == "Refund PDF"
    assert result["trace_id"]


async def test_docx_table_content_is_retrievable(world):
    doc = await world.ingest(await world.upload("pricing.docx", builders.docx_with_tables(), DOCX_TYPE, title="Pricing"))
    assert doc["status"] == "ready"
    result = await world.retrieve("Enterprise plan unlimited seats contact sales")
    assert result["status"] == "ok" and "Contact sales" in _texts(result) and result["sources"] == ["Pricing"]


async def test_html_content_is_retrievable_and_boilerplate_is_not_indexed(world):
    doc = await world.ingest(await world.upload("help.html", builders.HTML_PAGE, "text/html", title="Help page"))
    assert doc["status"] == "ready"
    result = await world.retrieve("return item within 30 days electronics 15 days label")
    assert result["status"] == "ok" and "15 days" in _texts(result)
    for noise in ("LOGIN-NAV-LINK", "COOKIE-BANNER-TEXT", "FOOTER-BOILERPLATE", "DO-NOT-INDEX-TRACKER"):
        assert noise not in _texts(await world.retrieve(noise.replace("-", " ")))


async def test_csv_row_is_found_by_its_identifier(world):
    doc = await world.ingest(await world.upload("catalog.csv", builders.CSV_DATA, "text/csv", title="Catalog"))
    assert doc["status"] == "ready"
    result = await world.retrieve("SKU0042 Widget 42 price")
    assert result["status"] == "ok" and "SKU0042" in _texts(result)


async def test_markdown_child_hit_expands_to_its_parent_section(world):
    doc = await world.ingest(await world.upload("handbook.md", MARKDOWN, "text/markdown", title="Handbook"))
    assert doc["status"] == "ready"
    assert await world.pool.fetchval(
        "SELECT count(*) FROM kb_chunks WHERE document_id = $1 AND is_parent", doc["id"]) >= 2

    result = await world.retrieve("topic2x paragraph")
    assert result["status"] == "ok"
    content = _texts(result)
    # The query names one paragraph; the answer carries its siblings because the parent section comes back.
    assert "topic2x" in content and "topic0x" in content and "topic4x" in content
    top = result["chunks"][0]  # one section, returned once, not three overlapping children
    assert "topic2x" in top["content"] and "topic0x" in top["content"] and top["source"]["section_path"]
    assert sum("topic2x" in c["content"] for c in result["chunks"]) == 1
    listed = (await world.client.get(f"/knowledge-bases/{world.a.kb['id']}/documents",
                                     headers=world.auth(world.a.admin_token))).json()
    assert listed[0]["chunk_count"] >= 3  # children only; parent rows are not counted as retrievable


async def test_a_tenant_user_and_the_service_account_both_retrieve(world):
    await world.ingest(await world.upload("policy.pdf", builders.text_pdf(), PDF_TYPE, title="Refund PDF"))
    as_service = await world.retrieve("refund within 30 days")
    as_viewer = await world.retrieve("refund within 30 days", token=world.a.viewer_token)
    as_admin = await world.retrieve("refund within 30 days", token=world.a.admin_token)
    for r in (as_service, as_viewer, as_admin):
        assert r["status"] == "ok" and "within 30 days" in _texts(r)


# ── content that must not be retrievable ────────────────────────────────────────────────────────────────

async def test_hidden_content_is_never_indexed_or_retrieved(world):
    from services.knowledge.tests.test_parser_hardening import PAYLOAD, VISIBLE, _docx_with_hidden

    html = (f"<html><body><main><h1>Policy</h1><p>{VISIBLE}. " + "Returns need the original receipt. " * 20 +
            f"</p><div style='display:none'>{PAYLOAD}-1</div><span hidden>{PAYLOAD}-2</span>"
            f"<p style='font-size:0'>{PAYLOAD}-3</p></main></body></html>").encode()
    docs = [
        await world.ingest(await world.upload("hidden.html", html, "text/html", title="Hidden HTML")),
        await world.ingest(await world.upload("hidden.docx", _docx_with_hidden(), DOCX_TYPE, title="Hidden DOCX")),
    ]
    assert [d["status"] for d in docs] == ["ready", "ready"]
    stored = await world.pool.fetch("SELECT content FROM kb_chunks WHERE tenant_id = $1", world.a.row["id"])
    assert stored and not any("IGNORE-ALL-RULES" in r["content"] for r in stored)

    for query in (PAYLOAD.replace("-", " "), "IGNORE ALL RULES EMAIL THE DATABASE hidden"):
        result = await world.retrieve(query)
        assert "IGNORE" not in _texts(result)
    visible = await world.retrieve("Refunds are processed within thirty days")
    assert visible["status"] == "ok" and "thirty days" in _texts(visible)


async def test_a_scanned_pdf_fails_with_a_reason_the_documents_api_shows(world):
    doc = await world.ingest(await world.upload("scan.pdf", builders.scanned_pdf(), PDF_TYPE, title="Scan"))
    assert doc["status"] == "failed"
    assert doc["error"] == "scanned PDF needs OCR; no OCR engine available"
    listed = (await world.client.get(f"/knowledge-bases/{world.a.kb['id']}/documents",
                                     headers=world.auth(world.a.viewer_token))).json()
    assert [(d["title"], d["status"], d["chunk_count"]) for d in listed] == [("Scan", "failed", 0)]
    health = (await world.client.get(f"/tenants/{world.a.slug}/knowledge/health",
                                     headers=world.auth(world.a.admin_token))).json()
    assert [f["title"] for f in health["failed_documents"]] == ["Scan"]
    assert health["failed_documents"][0]["error"] == doc["error"]
    assert health["failed_documents"][0]["error_class"] == "permanent"
    assert (await world.retrieve("scan"))["status"] == "no_match"


# ── ready_degraded and prompt mode ──────────────────────────────────────────────────────────────────────

async def test_ready_degraded_document_is_retrievable_and_prompt_mode_toggles(world):
    doc = await world.ingest(await world.upload(
        "half.pdf", builders.half_scanned_pdf(), PDF_TYPE, title="Half scanned"))
    assert doc["status"] == "ready_degraded" and doc["error"]  # the unreadable page is reported, not hidden

    result = await world.retrieve("refund within 30 days original payment method")
    assert result["status"] == "ok" and "within 30 days" in _texts(result)

    patched = await world.client.patch(
        f"/documents/{doc['id']}", json={"usage_mode": "prompt"}, headers=world.auth(world.a.admin_token))
    assert patched.status_code == 200 and patched.json()["usage_mode"] == "prompt"
    unrelated = await world.retrieve("zzqx plugh")  # nothing matches, but a prompt-mode document is always included
    assert unrelated["status"] == "ok" and unrelated["retrieval_metadata"]["always_included"] == 1
    assert "refund" in _texts(unrelated).lower()

    back = await world.client.patch(
        f"/documents/{doc['id']}", json={"usage_mode": "auto"}, headers=world.auth(world.a.admin_token))
    assert back.status_code == 200
    assert (await world.retrieve("zzqx plugh"))["status"] == "no_match"
    assert "within 30 days" in _texts(await world.retrieve("refund within 30 days original payment method"))


# ── dependencies failing ────────────────────────────────────────────────────────────────────────────────

async def test_embedder_down_answers_from_lexical_search_as_partial_not_an_error(world):
    await world.ingest(await world.upload("handbook.md", MARKDOWN, "text/markdown", title="Handbook"))
    world.embedder.fail = True

    result = await world.retrieve("topic2x paragraph")
    assert result["status"] == "partial"
    assert "embedding_unavailable" in result["degraded"] and "lexical_only" in result["degraded"]
    assert "topic2x" in _texts(result)  # still a real answer


async def test_embedder_down_with_dense_only_policy_still_answers_lexically(world):
    await world.ingest(await world.upload("handbook.md", MARKDOWN, "text/markdown", title="Handbook"))
    world.embedder.fail = True
    result = await world.retrieve("topic2x paragraph", hybrid_search=False)
    assert result["status"] in ("partial", "ok") and "topic2x" in _texts(result)


async def test_overload_is_a_typed_unavailable_and_recovers(world):
    await world.ingest(await world.upload("policy.pdf", builders.text_pdf(), PDF_TYPE, title="Refund PDF"))
    cfg = dataclasses.replace(retrieval._config, max_inflight=1, tenant_max_inflight=1, queue_wait_s=0.0)
    retrieval._config = cfg
    before = ops_metrics.snapshot()["retrieval_overloaded"]
    try:
        assert await admission.acquire(world.a.slug, cfg, 0.0)  # every slot taken
        refused = await world.retrieve("refund within 30 days")
        admission.release(world.a.slug)
    finally:
        retrieval._config = dataclasses.replace(cfg, max_inflight=5, tenant_max_inflight=3, queue_wait_s=0.15)
    assert refused["status"] == "unavailable" and refused["degraded"] == ["overloaded"] and refused["chunks"] == []
    assert ops_metrics.snapshot()["retrieval_overloaded"] == before + 1
    assert admission.in_flight == 0
    assert (await world.retrieve("refund within 30 days"))["status"] == "ok"


async def test_an_exhausted_connection_pool_is_unavailable_not_a_hang(world):
    await world.ingest(await world.upload("policy.pdf", builders.text_pdf(), PDF_TYPE, title="Refund PDF"))
    retrieval.clear_caches()  # a cold plan needs a connection, and there are none
    pool = await db.get_pool()
    held = [await pool.acquire() for _ in range(pool.get_max_size())]
    try:
        with pytest.raises(RetrievalUnavailable) as err:
            await asyncio.wait_for(retrieval.retrieve(
                None, await get_vector_repo(),
                make_manager(world.embedder), tenant_slug=world.a.slug, agent_slug=world.a.agent["slug"],
                query="refund within 30 days"), timeout=2.0)
        assert err.value.degraded == ["overloaded"]
    finally:
        for conn in held:
            await pool.release(conn)
    assert admission.in_flight == 0
    assert (await world.retrieve("refund within 30 days"))["status"] == "ok"


# ── tenant isolation, end to end ────────────────────────────────────────────────────────────────────────

async def test_one_tenants_query_never_returns_another_tenants_content(world):
    # Same agent slug and KB slug in both tenants: only the tenant scope separates them.
    b = await world.make_tenant("b")
    assert b.agent["slug"] == world.a.agent["slug"] and b.kb["slug"] == world.a.kb["slug"]
    secret_a = ("Tenant Alpha confidential rate card: the platinum surcharge is forty seven dollars. "
                + "Alpha ledger entries are reconciled nightly by the finance bot. " * 12).encode()
    secret_b = ("Tenant Bravo confidential rate card: the platinum surcharge is ninety one dollars. "
                + "Bravo ledger entries are reconciled weekly by the audit team. " * 12).encode()
    await world.ingest(await world.upload("rates.txt", secret_a, "text/plain", title="Alpha rates"))
    await world.ingest(await world.upload("rates.txt", secret_b, "text/plain", tenant=b, title="Bravo rates"), tenant=b)

    for token in (world.service_token, b.viewer_token, b.admin_token):
        result = await world.retrieve("platinum surcharge rate card ledger", tenant=b, token=token)
        text = _texts(result)
        assert result["status"] == "ok" and "Bravo" in text and "ninety one" in text
        assert "Alpha" not in text and "forty seven" not in text and result["sources"] == ["Bravo rates"]
    mine = await world.retrieve("platinum surcharge rate card ledger")
    assert "Alpha" in _texts(mine) and "Bravo" not in _texts(mine) and mine["sources"] == ["Alpha rates"]

    # A tenant user naming the other tenant's slug is refused, with no content in the body.
    for foreign in (world.a.slug, str(world.a.row["id"])):
        resp = await world.client.post(
            "/internal/retrieve", json={"tenant_slug": foreign, "agent_slug": "sup", "query": "platinum surcharge"},
            headers=world.auth(b.viewer_token))
        assert resp.status_code in (403, 404) and "Alpha" not in resp.text
    # Documents, events and health are tenant-scoped too.
    docs_a = (await world.client.get(f"/knowledge-bases/{world.a.kb['id']}/documents",
                                     headers=world.auth(world.a.admin_token))).json()
    for path in (f"/documents/{docs_a[0]['id']}", f"/documents/{docs_a[0]['id']}/events",
                 f"/knowledge-bases/{world.a.kb['id']}/documents", f"/tenants/{world.a.slug}/knowledge/health"):
        resp = await world.client.get(path, headers=world.auth(b.admin_token))
        assert resp.status_code in (403, 404), (path, resp.status_code)
    # Ingestion state does not leak either: B's health counts none of A's jobs.
    await world.pool.execute("UPDATE kb_documents SET status = 'failed', error = 'alpha only' WHERE tenant_id = $1",
                             world.a.row["id"])
    health_b = (await world.client.get(f"/tenants/{b.slug}/knowledge/health", headers=world.auth(b.admin_token))).json()
    assert health_b["failed_documents"] == []


# ── re-ingestion, retries, dead letter ──────────────────────────────────────────────────────────────────

async def test_reingest_swaps_chunks_atomically_with_no_gap_for_readers(world):
    old = ("Legacy plan: the blue gadget ships from the Oslo depot. " + "Oslo depot handles blue gadget orders daily. " * 14).encode()
    doc = await world.ingest(await world.upload("ship.txt", old, "text/plain", title="Shipping"))
    assert "Oslo" in _texts(await world.retrieve("blue gadget Oslo depot"))
    before_ids = {r["id"] for r in await world.pool.fetch("SELECT id FROM kb_chunks WHERE document_id = $1", doc["id"])}

    new = ("Current plan: the blue gadget ships from the Lima hub. " + "Lima hub handles blue gadget orders daily. " * 14).encode()
    await asyncio.to_thread(Path(doc["source_ref"]).write_bytes, new)
    await world.pool.execute("UPDATE kb_documents SET version = version + 1 WHERE id = $1", doc["id"])
    job_id = await world.pool.fetchval(
        "INSERT INTO kb_ingestion_jobs (document_id, kb_id) VALUES ($1, $2) RETURNING id", doc["id"], world.a.kb["id"])

    observed: list[int] = []
    stop = asyncio.Event()

    async def reader() -> None:
        while not stop.is_set():
            observed.append(await world.pool.fetchval(
                "SELECT count(*) FROM kb_chunks WHERE document_id = $1 AND NOT is_parent", doc["id"]))
            await asyncio.sleep(0)

    task = asyncio.create_task(reader())
    await world.ingest({**doc, "ingestion_job_id": job_id})
    stop.set()
    await task

    assert len(observed) > 3 and min(observed) > 0, observed  # never an empty document while it was re-indexed
    after_ids = {r["id"] for r in await world.pool.fetch("SELECT id FROM kb_chunks WHERE document_id = $1", doc["id"])}
    assert after_ids and not (before_ids & after_ids)
    fresh = await world.retrieve("blue gadget ships from")
    assert "Lima" in _texts(fresh) and "Oslo" not in _texts(fresh)


async def test_transient_failure_is_retried_and_the_document_becomes_ready(world):
    doc = await world.upload("handbook.md", MARKDOWN, "text/markdown", title="Handbook")
    await _drain_pending(world.pool)
    await world.pool.execute("UPDATE kb_ingestion_jobs SET next_attempt_at = now() WHERE id = $1", doc["ingestion_job_id"])
    world.embedder.fail = True
    _clear_scope()
    job = await _claim_next_job(world.pool, tenant_cap=100)
    assert job and str(job["id"]) == str(doc["ingestion_job_id"])
    await process_one_job(world.pool, LocalStorageProvider(), make_manager(world.embedder), job)
    state = await world.document(doc["id"])
    assert state["status"] == "pending"  # waiting for its retry, not lost and not failed

    world.embedder.fail = False
    await world.pool.execute("UPDATE kb_ingestion_jobs SET next_attempt_at = now() WHERE id = $1", doc["ingestion_job_id"])
    job = await _claim_next_job(world.pool, tenant_cap=100)
    assert job and job["attempts"] == 2
    await process_one_job(world.pool, LocalStorageProvider(), make_manager(world.embedder), job)
    assert (await world.document(doc["id"]))["status"] == "ready"
    events = (await world.client.get(f"/documents/{doc['id']}/events", headers=world.auth(world.a.admin_token))).json()
    assert [e["status"] for e in events if e["stage"] == "worker"] == ["retry_scheduled"]
    assert events[-1]["stage"] == "index" and events[-1]["status"] == "ok"


async def test_dead_letter_is_visible_with_its_reason_and_can_be_retried(world):
    doc = await world.upload("handbook.md", MARKDOWN, "text/markdown", title="Handbook")
    await _drain_pending(world.pool)
    await world.pool.execute(
        "UPDATE kb_ingestion_jobs SET max_attempts = 2, next_attempt_at = now() WHERE id = $1", doc["ingestion_job_id"])
    world.embedder.fail = True
    for _ in range(2):
        _clear_scope()
        job = await _claim_next_job(world.pool, tenant_cap=100)
        assert job
        await process_one_job(world.pool, LocalStorageProvider(), make_manager(world.embedder), job)
        await world.pool.execute("UPDATE kb_ingestion_jobs SET next_attempt_at = now() WHERE id = $1", job["id"])

    state = await world.document(doc["id"])
    assert state["status"] == "failed" and "RuntimeError" in state["error"]
    assert "hunter2" not in state["error"] and "sk-secret" not in state["error"]  # credentials never reach the API

    health = (await world.client.get(f"/tenants/{str(world.a.row['id'])}/knowledge/health",
                                     headers=world.auth(world.a.viewer_token))).json()
    assert health["queue"]["dead_letter"] == 1 and health["queue"]["pending"] == 0
    assert health["queue"]["stuck_running"] == 0
    failed = health["failed_documents"]
    assert len(failed) == 1 and failed[0]["job_status"] == "dead_letter" and failed[0]["error_class"] == "transient"
    assert "RuntimeError" in failed[0]["error"] and "hunter2" not in failed[0]["error"] and failed[0]["attempts"] == 2
    events = (await world.client.get(f"/documents/{doc['id']}/events", headers=world.auth(world.a.viewer_token))).json()
    assert events[-1]["stage"] == "worker" and events[-1]["status"] == "dead_letter"
    assert "hunter2" not in str(events)

    # The existing retry route re-queues it; with the embedder back it reaches ready and retrieves.
    world.embedder.fail = False
    retried = await world.client.post(f"/documents/{doc['id']}/retry", headers=world.auth(world.a.admin_token))
    assert retried.status_code == 200 and retried.json()["status"] == "pending"
    viewer_retry = await world.client.post(f"/documents/{doc['id']}/retry", headers=world.auth(world.a.viewer_token))
    assert viewer_retry.status_code == 403
    done = await world.ingest({"id": doc["id"], "ingestion_job_id": retried.json()["ingestion_job_id"]})
    assert done["status"] == "ready" and done["error"] is None
    assert "topic2x" in _texts(await world.retrieve("topic2x paragraph"))
    health = (await world.client.get(f"/tenants/{world.a.slug}/knowledge/health",
                                     headers=world.auth(world.a.admin_token))).json()
    assert health["failed_documents"] == [] and health["queue"]["dead_letter"] == 1  # history stays countable


# ── ops visibility ──────────────────────────────────────────────────────────────────────────────────────

async def test_health_reports_retrieval_stats_and_queue_depth(world):
    await world.ingest(await world.upload("policy.pdf", builders.text_pdf(), PDF_TYPE, title="Refund PDF"))
    for _ in range(3):
        assert (await world.retrieve("refund within 30 days"))["status"] == "ok"
    assert (await world.retrieve("zzqx plugh"))["status"] == "no_match"
    world.embedder.fail = True
    assert (await world.retrieve("refund within 30 days original"))["status"] == "partial"
    queued = await world.upload("later.txt", b"Queued document text. " * 40, "text/plain", title="Queued")
    await retrieval_log.flush()

    health = (await world.client.get(f"/tenants/{world.a.slug}/knowledge/health",
                                     headers=world.auth(world.a.admin_token))).json()
    stats = health["retrieval_24h"]
    assert stats["total"] == 5 and stats["by_status"]["ok"] == 3
    assert stats["by_status"]["no_match"] == 1 and stats["by_status"]["partial"] == 1
    assert stats["no_match_rate"] == 0.2 and stats["degraded"] >= 1
    assert stats["degraded_by_reason"].get("embedding_unavailable") == 1
    latency = stats["latency_ms"]
    assert 0 < latency["p50"] <= latency["p95"] <= latency["p99"] < 400
    assert health["queue"]["pending"] == 1 and health["queue"]["oldest_pending_age_s"] >= 0
    assert queued["status"] == "pending"


async def test_ops_counters_are_platform_only_and_carry_no_tenant_data(world):
    await world.ingest(await world.upload("policy.pdf", builders.text_pdf(), PDF_TYPE, title="Refund PDF"))
    await world.retrieve("refund within 30 days")
    await world.retrieve("refund within 30 days")
    resp = await world.client.get("/internal/ops", headers=world.auth(world.service_token))
    assert resp.status_code == 200
    snap = resp.json()
    assert snap["plan_cache_miss"] >= 1 and snap["plan_cache_hit"] >= 1 and snap["retrieval_in_flight"] == 0
    assert "closed" in snap["breakers"].get("embed", {"closed": 0})
    prom = (await world.client.get("/internal/ops?format=prometheus", headers=world.auth(world.service_token))).text
    assert "knowledge_plan_cache_total{result=\"hit\"}" in prom
    for body in (resp.text, prom):
        assert world.a.slug not in body and str(world.a.row["id"]) not in body and str(world.a.kb["id"]) not in body
    assert (await world.client.get("/internal/ops", headers=world.auth(world.a.admin_token))).status_code == 403


# ── the conversation-side tool, against the real knowledge app ─────────────────────────────────────────

async def test_search_knowledge_tool_envelope_over_the_real_service(world, test_service_viewer):
    from libs.knowledge_sdk.providers.cache_aside import CacheAsideKnowledgeProvider
    from libs.knowledge_sdk.repositories.http_repository import HttpKnowledgeRepository
    from services.conversation.tests.test_pipeline import _make_handler, _make_llm, _make_stt, _make_tts
    from services.conversation.tools.envelope import build_envelope

    def login(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": test_service_viewer["token"]})

    repo = HttpKnowledgeRepository(
        "http://knowledge", "http://config", "svc@example.com", "pw",
        transport=ASGITransport(app=app), auth_transport=httpx.MockTransport(login))
    provider = CacheAsideKnowledgeProvider(repo, repo)  # what the conversation service runs in production
    handler = _make_handler(_make_stt("hi"), _make_llm(["ok"]), _make_tts(), knowledge=provider, node_knowledge=["kb1"])
    handler._tenant_slug, handler._agent_slug = world.a.slug, world.a.agent["slug"]
    _definition, search = handler._local_tools([], None, "s1")["search_knowledge"]

    async def call(query: str) -> dict:
        result = await search({"query": query})
        return build_envelope(result, read_only=True, latency_ms=1.0, trace_id="t")

    try:
        await world.ingest(await world.upload("handbook.md", MARKDOWN, "text/markdown", title="Handbook"))
        ok = await call("topic2x paragraph")
        assert ok["status"] == "ok" and ok["error"] is None and ok["data"]["found"] is True
        assert "topic2x" in " ".join(ok["data"]["passages"]) and ok["data"]["sources"] == ["Handbook"]

        none = await call("zzqx plugh")
        assert none["status"] == "no_match" and none["data"] == {"found": False, "passages": []}

        world.embedder.fail = True
        partial = await call("topic3x paragraph")
        assert partial["status"] == "partial" and "lexical_only" in partial["data"]["degraded"]
        world.embedder.fail = False

        cfg = dataclasses.replace(retrieval._config, max_inflight=1, tenant_max_inflight=1, queue_wait_s=0.0)
        retrieval._config = cfg
        try:
            assert await admission.acquire(world.a.slug, cfg, 0.0)
            unavailable = await call("topic2x paragraph")
            admission.release(world.a.slug)
        finally:
            retrieval._config = dataclasses.replace(cfg, max_inflight=5, tenant_max_inflight=3, queue_wait_s=0.15)
        assert unavailable["status"] == "unavailable" and unavailable["error"]["code"] == "knowledge_unavailable"
        assert unavailable["error"]["user_safe_message"] and "overloaded" not in unavailable["error"]["user_safe_message"]
        assert (await call("topic2x paragraph"))["status"] == "ok"  # and it recovers

        # The knowledge service itself unreachable: the same typed status, never an exception into the turn.
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        down = HttpKnowledgeRepository(
            "http://knowledge", "http://config", "svc@example.com", "pw",
            transport=httpx.MockTransport(refuse), auth_transport=httpx.MockTransport(login))
        handler._knowledge = CacheAsideKnowledgeProvider(down, down)
        gone = await call("topic2x paragraph")
        assert gone["status"] == "unavailable" and gone["error"]["user_safe_message"]
        await down.close()
    finally:
        await repo.close()
