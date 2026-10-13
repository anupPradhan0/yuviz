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
from services.knowledge.vector_repository import PgVectorRepository
from services.knowledge.storage import LocalStorageProvider
from services.knowledge.tests.eval.golden import ALL_DOCS, QUESTIONS
from services.knowledge.tests.eval.harness import Metrics, _norm
from services.knowledge.tests.rag_fakes import FakeEmbedder, make_kb, make_manager

TOP_K = 5
_BENIGN = frozenset({"plan_cache_miss", "prompt_docs_trimmed"})  # not fallbacks: a cold plan, and tiny prompt-mode docs
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


class ExactRepo(PgVectorRepository):
    """Dense search by exact scan, and a total order on equal scores, so a run depends on the data and the code only.

    Two things made the same golden set score 0.72, 0.69 or 0.67 in different runs: the approximate HNSW index keeps
    dead entries from every earlier insert and delete until a VACUUM, and the templated distractor documents contain
    identical chunks whose equal scores (cosine distance, ts_rank_cd) come back in physical row order, which differs on
    a database other tests have churned. Exact scan fixes the first; fetching a wider candidate list and ordering ties
    by (title, content) fixes the second. This eval measures the pipeline, not index health or tie order."""

    _WIDE = 200

    @staticmethod
    def _stable(matches, limit):
        return sorted(matches, key=lambda m: (-m.score, m.document_title, m.child_content or m.content))[:limit]

    async def search(self, conn, kb_ids, embedding, top_k, minimum_score, *args, **kwargs):
        # *args/**kwargs: model and notes (and whatever the real signature grows next) pass straight through.
        await conn.execute("SET LOCAL enable_indexscan = off")
        wide = await super().search(conn, kb_ids, embedding, max(top_k, self._WIDE), minimum_score, *args, **kwargs)
        return self._stable(wide, top_k)

    async def lexical_search(self, conn, kb_ids, query, limit, minimum_score, *args, **kwargs):
        wide = await super().lexical_search(conn, kb_ids, query, max(limit, self._WIDE), minimum_score, *args, **kwargs)
        return self._stable(wide, limit)


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
    previous_tenants = os.environ.get("KNOWLEDGE_ENRICH_TENANTS")
    if llm_config:  # enrichment is per-tenant opt-in: enable exactly this throwaway tenant
        os.environ["KNOWLEDGE_ENRICH_TENANTS"] = slug
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
        repo = ExactRepo()
        m, missed, degraded = Metrics(), [], []
        reordered, rerank_ms, degraded_seen = 0, [], set()
        for q in QUESTIONS:
            set_caller_tenant(str(tenant["id"]))
            result = await retrieval.retrieve(
                None, repo, manager, tenant_slug=slug, agent_slug="golden", query=q.question, top_k=TOP_K,
                minimum_score=MINIMUM_SCORE, config=QUALITY_CONFIG,
                rerank=reranker is not None or None, reranker=reranker)
            if reranker is not None:
                plain = await retrieval.retrieve(
                    None, repo, manager, tenant_slug=slug, agent_slug="golden", query=q.question, top_k=TOP_K,
                    minimum_score=MINIMUM_SCORE, config=QUALITY_CONFIG, rerank=False)
                reordered += [c["source"]["chunk_id"] for c in result["chunks"]] != [
                    c["source"]["chunk_id"] for c in plain["chunks"]]
                rerank_ms.append(result["retrieval_metadata"]["timings_ms"].get("rerank", 0.0))
                degraded_seen.update(result["degraded"])
            # Prompt-mode documents (tiny files auto-inline) come first and are always returned: an answer there
            # counts as rank 1, and they do not push the ranked matches down.
            always = result["retrieval_metadata"]["always_included"]
            degraded += [d for d in result["degraded"] if d not in _BENIGN]
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
            **({"enriched_chunks": enrich["enriched"], "enrich_failed": enrich["failed"]} if llm_config else {}),
            **({"reordered": reordered, "rerank_ms": rerank_ms, "rerank_degraded": sorted(degraded_seen)} if reranker else {}),
        }
    finally:
        if previous_tenants is None:
            os.environ.pop("KNOWLEDGE_ENRICH_TENANTS", None)
        else:
            os.environ["KNOWLEDGE_ENRICH_TENANTS"] = previous_tenants
        if previous is None:
            os.environ.pop("KNOWLEDGE_CHUNKER", None)
        else:
            os.environ["KNOWLEDGE_CHUNKER"] = previous
        set_caller_tenant(None)
        set_target_tenant(None)
        await _purge(pool, tenant["id"])
