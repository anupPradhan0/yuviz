"""The golden set run through the whole pipeline: upload, worker (parse, chunk, embed, index) and retrieve(), on a
real Postgres with the deterministic bag-of-words embedder from rag_fakes. Where harness.py scores chunking in
memory, this scores what a caller would actually get back: fusion, parent expansion, the token budget and the
minimum score are all in the loop. Fake embedder, so the numbers catch regressions; they do not predict the real
model's scores."""

from __future__ import annotations

import os
import uuid

from libs.tenancy import set_caller_tenant, set_target_tenant
from services.knowledge import documents as documents_service
from services.knowledge import retrieval, retrieval_log
from services.knowledge.ingestion_worker import process_one_job
from services.knowledge.resilience import RetrievalConfig
from services.knowledge.runtime import get_vector_repo
from services.knowledge.storage import LocalStorageProvider
from services.knowledge.tests.eval.golden import ALL_DOCS, QUESTIONS
from services.knowledge.tests.eval.harness import Metrics, _norm
from services.knowledge.tests.rag_fakes import FakeEmbedder, make_kb, make_manager

TOP_K = 5
# The default minimum_score (0.3) is a cosine threshold tuned for real embedding models; the bag-of-words fake scores
# lower, so under it the dense branch returned nothing and the eval was lexical-only, where equal ts_rank scores
# tie in arbitrary order (one golden question flipped between runs). With no floor both branches rank and RRF
# fusion breaks the ties.
MINIMUM_SCORE = 0.0
# This eval measures answer quality, not latency. Under the production budget (120 ms per branch) a loaded machine
# times a branch out now and then, the answer silently becomes lexical-only or dense-only, and one question flips
# between runs. Generous timeouts make a run depend on the data and the code only; the result also reports any
# degraded answer so a real fallback cannot hide inside the numbers.
QUALITY_CONFIG = RetrievalConfig(
    embed_timeout_s=10.0, dense_timeout_s=10.0, lexical_timeout_s=10.0, rerank_timeout_s=10.0, deadline_s=30.0,
    queue_wait_s=10.0, acquire_timeout_s=10.0, breaker_failures=10**9)


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
        "DELETE FROM tenants WHERE id = $1",
    ):
        await pool.execute(sql, tenant_id)


async def run_pipeline_eval(pool, chunker: str = "auto") -> dict:
    """Index every golden document into one KB and ask every question. Returns recall@5, MRR and
    answer-in-context over the questions, plus the questions that missed (for the failure message)."""
    previous = os.environ.get("KNOWLEDGE_CHUNKER")
    os.environ["KNOWLEDGE_CHUNKER"] = chunker  # read per call by the chunker
    slug = f"golden-{uuid.uuid4().hex[:8]}"
    tenant = dict(await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ($1, $1) RETURNING *", slug))
    try:
        agent = dict(await pool.fetchrow(
            "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'golden', 'Golden') RETURNING *", tenant["id"]))
        set_caller_tenant(str(tenant["id"]))
        kb = await make_kb(pool, tenant, agent, "golden")
        embedder, storage = FakeEmbedder(), LocalStorageProvider()
        manager = make_manager(embedder)
        for name, (kind, text) in ALL_DOCS.items():
            ctype, ext = ("text/csv", "csv") if kind == "table" else ("text/markdown", "md")
            doc = await documents_service.upload_document(
                tenant_id=tenant["id"], kb_id=kb["id"], title=name, filename=f"{name}.{ext}", content_type=ctype,
                content=text.encode(), storage=storage)
            job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
            set_caller_tenant(None)
            await process_one_job(pool, storage, manager, job)
            set_caller_tenant(str(tenant["id"]))
        not_ready = await pool.fetchval(
            "SELECT count(*) FROM kb_documents WHERE tenant_id = $1 AND status NOT IN ('ready', 'ready_degraded')",
            tenant["id"])

        retrieval.clear_caches()
        repo = await get_vector_repo()
        m, missed, degraded = Metrics(), [], []
        for q in QUESTIONS:
            set_caller_tenant(str(tenant["id"]))
            result = await retrieval.retrieve(
                None, repo, manager, tenant_slug=slug, agent_slug="golden", query=q.question, top_k=TOP_K,
                minimum_score=MINIMUM_SCORE, config=QUALITY_CONFIG)
            # Prompt-mode documents (tiny files auto-inline) come first and are always returned: an answer there
            # counts as rank 1, and they do not push the ranked matches down.
            always = result["retrieval_metadata"]["always_included"]
            degraded += [d for d in result["degraded"] if d != "plan_cache_miss"]
            chunks = result["chunks"][:always + TOP_K]
            answer = _norm(q.answer)

            def holds(c):
                return c["source"]["document_title"] == q.doc and answer in _norm(c["content"])
            rank = 1 if any(holds(c) for c in chunks[:always]) else next(
                (i + 1 for i, c in enumerate(chunks[always:]) if holds(c)), None)
            context = _norm(" ".join(c["content"] for c in chunks))
            m.n += 1
            m.recall_at_5 += rank is not None
            m.mrr += 1 / rank if rank else 0.0
            m.answer_in_context += all(_norm(t) in context for t in q.terms)
            if rank is None:
                missed.append(q.question)
        return {
            "n": m.n, "documents_not_ready": not_ready, "recall_at_5": m.recall_at_5 / m.n, "mrr": m.mrr / m.n,
            "answer_in_context": m.answer_in_context / m.n, "missed": missed, "degraded": degraded,
        }
    finally:
        if previous is None:
            os.environ.pop("KNOWLEDGE_CHUNKER", None)
        else:
            os.environ["KNOWLEDGE_CHUNKER"] = previous
        set_caller_tenant(None)
        set_target_tenant(None)
        await _purge(pool, tenant["id"])
