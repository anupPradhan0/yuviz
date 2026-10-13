"""Run the golden set with a REAL embedder, and optionally a REAL reranker and REAL contextual enrichment, and print
recall@5 / MRR / answer-in-context side by side. This is the measurement the Phase 7 exit criterion needs; the
deterministic fakes in test_golden_gates.py only prove the plumbing.

    POSTGRES_DSN=postgresql://.../scratchdb OPENAI_API_KEY=... \\
    KNOWLEDGE_RERANKER=cohere COHERE_API_KEY=... \\
    python -m services.knowledge.tests.eval.real_eval --allow-external-calls [--rerank] [--enrich]

External calls happen ONLY with --allow-external-calls, and only to the providers you configured:
  embeddings   every golden chunk and question text -> OpenAI embeddings (text-embedding-3-small, 768 dims)
  --rerank     each question + its top candidate chunks -> the KNOWLEDGE_RERANKER provider (cohere | voyage)
  --enrich     each golden chunk, title, section and neighbours -> OpenAI chat (OPENAI_ENRICH_MODEL, default gpt-4o-mini)
The corpus is the synthetic Acme golden set (golden.py), not tenant data. Use a SCRATCH database: it creates and
deletes its own tenant. The golden set is small (39 questions): treat differences of a few points as noise and
confirm on real tenant questions before enabling either feature."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from services.knowledge import db, enrichment
from services.knowledge.embedding_manager import OpenAIEmbeddingProvider
from services.knowledge.reranker import build_reranker
from services.knowledge.tests.eval.pipeline_eval import run_pipeline_eval

_COLS = ("recall_at_5", "mrr", "answer_in_context")


def _row(label: str, r: dict) -> str:
    extra = ""
    if r.get("rerank_ms"):
        ms = sorted(r["rerank_ms"])
        extra = f"  rerank p50={ms[len(ms) // 2]:.0f}ms p95={ms[int(len(ms) * 0.95)]:.0f}ms degraded={r['degraded']}"
    if "enriched_chunks" in r:
        extra += f"  enriched={r['enriched_chunks']} failed={r['enrich_failed']}"
    return f"{label:<22}" + "  ".join(f"{c}={r[c]:.3f}" for c in _COLS) + extra


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--allow-external-calls", action="store_true", help="required: sends golden text to the providers")
    p.add_argument("--rerank", action="store_true", help="also run with the KNOWLEDGE_RERANKER provider")
    p.add_argument("--enrich", action="store_true", help="also run with contextual enrichment (OpenAI chat)")
    args = p.parse_args(argv)
    if not args.allow_external_calls:
        print("refusing to run: pass --allow-external-calls to send the golden corpus to the configured providers")
        return 2
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set (the real embedder)")
        return 2
    reranker = None
    if args.rerank:
        reranker = build_reranker()
        if reranker is None:
            print("--rerank needs KNOWLEDGE_RERANKER=cohere|voyage and that provider's key")
            return 2
    embedder = OpenAIEmbeddingProvider(os.environ["OPENAI_API_KEY"])
    pool = await db.get_pool()
    try:
        print(_row("baseline", await run_pipeline_eval(pool, embedder=embedder)))
        if reranker is not None:
            print(_row("+ rerank", await run_pipeline_eval(pool, reranker, embedder=embedder)))
        if args.enrich:
            os.environ["KNOWLEDGE_ENRICH_CHUNKS"] = "1"
            enrichment._test_transport = None
            llm = {"engine": "openai", "model": os.environ.get("OPENAI_ENRICH_MODEL", "gpt-4o-mini"),
                   "api_key_ref": "env:OPENAI_API_KEY"}
            print(_row("+ enrichment", await run_pipeline_eval(pool, embedder=embedder, llm_config=llm)))
    finally:
        if reranker is not None:
            await reranker.aclose()
        await db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
