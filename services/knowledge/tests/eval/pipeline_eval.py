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
from services.knowledge.runtime import get_vector_repo
from services.knowledge.storage import LocalStorageProvider
from services.knowledge.tests.eval.golden import ALL_DOCS, QUESTIONS
from services.knowledge.tests.eval.harness import Metrics, _norm
from services.knowledge.tests.rag_fakes import FakeEmbedder, make_kb, make_manager

TOP_K = 5


async def _purge(pool, tenant_id) -> None:
    await retrieval_log.flush()
    await pool.execute("UPDATE tenants SET default_llm_config_id = NULL WHERE id = $1", tenant_id)
    for sql in (
        "DELETE FROM retrieval_logs WHERE tenant_id = $1",
        "DELETE FROM kb_chunks WHERE tenant_id = $1",
        "DELETE FROM kb_ingestion_events WHERE tenant_id = $1",
        "DELETE FROM kb_ingestion_jobs WHERE document_id IN (SELECT id FROM kb_documents WHERE tenant_id = $1)",
        "DELETE FROM audit_log WHERE tenant_id = $1",
        "DELETE FROM kb_documents WHERE tenant_id = $1",
        "DELETE FROM agent_retrieval_policies WHERE agent_id IN (SELECT id FROM agents WHERE tenant_id = $1)",
        "DELETE FROM agent_knowledge_bases WHERE agent_id IN (SELECT id FROM agents WHERE tenant_id = $1)",
        "DELETE FROM knowledge_bases WHERE tenant_id = $1",
        "DELETE FROM agents WHERE tenant_id = $1",
        "DELETE FROM provider_configs WHERE tenant_id = $1",
        "DELETE FROM tenants WHERE id = $1",
    ):
        await pool.execute(sql, tenant_id)


async def run_pipeline_eval(
    pool, chunker: str = "auto", reranker=None, *, embedder=None, llm_config: dict | None = None,
) -> dict:
    """Index every golden document into one KB and ask every question. Returns recall@5, MRR and
    answer-in-context over the questions, plus the questions that missed (for the failure message). With a
    `reranker`, every question runs with rerank=True and the result also reports how many questions got a different
    chunk order and the rerank stage's latency. `embedder` replaces the deterministic fake (real_eval.py), and
    `llm_config` ({engine, model, api_key_ref}) gives the eval tenant an LLM provider for contextual enrichment."""
    previous = os.environ.get("KNOWLEDGE_CHUNKER")
    os.environ["KNOWLEDGE_CHUNKER"] = chunker  # read per call by the chunker
    slug = f"golden-{uuid.uuid4().hex[:8]}"
    tenant = dict(await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ($1, $1) RETURNING *", slug))
    try:
        agent = dict(await pool.fetchrow(
            "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'golden', 'Golden') RETURNING *", tenant["id"]))
        set_caller_tenant(str(tenant["id"]))
        if reranker is not None:  # only the agent's policy can switch rerank on
            await pool.execute("INSERT INTO agent_retrieval_policies (agent_id, rerank) VALUES ($1, true)", agent["id"])
        kb = await make_kb(pool, tenant, agent, "golden")
        if llm_config:
            cfg_id = await pool.fetchval(
                "INSERT INTO provider_configs (tenant_id, name, role, engine, model, api_key_ref) "
                "VALUES ($1, 'eval-llm', 'llm', $2, $3, $4) RETURNING id", tenant["id"], llm_config["engine"],
                llm_config.get("model"), llm_config["api_key_ref"])
            await pool.execute("UPDATE tenants SET default_llm_config_id = $2 WHERE id = $1", tenant["id"], cfg_id)
        embedder, storage = embedder or FakeEmbedder(), LocalStorageProvider()
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

        enrich = await pool.fetchrow(
            "SELECT coalesce(sum((detail->>'enriched')::int), 0) AS enriched, coalesce(sum((detail->>'failed')::int), 0) AS failed "
            "FROM kb_ingestion_events WHERE tenant_id = $1 AND stage = 'embed' AND status = 'ok'", tenant["id"])
        retrieval.clear_caches()
        repo = await get_vector_repo()
        m, missed = Metrics(), []
        reordered, rerank_ms, degraded_seen = 0, [], set()
        for q in QUESTIONS:
            set_caller_tenant(str(tenant["id"]))
            result = await retrieval.retrieve(
                None, repo, manager, tenant_slug=slug, agent_slug="golden", query=q.question, top_k=TOP_K,
                rerank=reranker is not None or None, reranker=reranker)
            if reranker is not None:
                plain = await retrieval.retrieve(
                    None, repo, manager, tenant_slug=slug, agent_slug="golden", query=q.question, top_k=TOP_K,
                    rerank=False)
                reordered += [c["source"]["chunk_id"] for c in result["chunks"]] != [
                    c["source"]["chunk_id"] for c in plain["chunks"]]
                rerank_ms.append(result["retrieval_metadata"]["timings_ms"].get("rerank", 0.0))
                degraded_seen.update(result["degraded"])
            # Prompt-mode documents (tiny files auto-inline) come first and are always returned: an answer there
            # counts as rank 1, and they do not push the ranked matches down.
            always = result["retrieval_metadata"]["always_included"]
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
            "answer_in_context": m.answer_in_context / m.n, "missed": missed,
            **({"enriched_chunks": enrich["enriched"], "enrich_failed": enrich["failed"]} if llm_config else {}),
            **({"reordered": reordered, "rerank_ms": rerank_ms, "degraded": sorted(degraded_seen)} if reranker else {}),
        }
    finally:
        if previous is None:
            os.environ.pop("KNOWLEDGE_CHUNKER", None)
        else:
            os.environ["KNOWLEDGE_CHUNKER"] = previous
        set_caller_tenant(None)
        set_target_tenant(None)
        await _purge(pool, tenant["id"])
