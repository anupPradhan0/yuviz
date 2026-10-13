"""Golden-set regression gates (see gates.py): a drop in recall@5, MRR or answer-in-context below the stored
baseline minus the tolerance fails the build. Refresh the baseline deliberately with
`python -m services.knowledge.tests.eval.gates --update-baseline`."""

from __future__ import annotations

from services.knowledge.tests.eval import gates
from services.knowledge.tests.eval.golden import ALL_DOCS, QUESTIONS
from services.knowledge.tests.eval.pipeline_eval import run_pipeline_eval


def test_baseline_matches_the_golden_set_it_was_measured_on():
    baseline = gates.load_baseline()
    assert baseline["questions"] == len(QUESTIONS) and baseline["documents"] == len(ALL_DOCS), (
        "the golden corpus changed: refresh the baseline on purpose (see gates.py)")
    assert 0 < baseline["tolerance"] <= 0.1  # a wide tolerance would make the gate decorative


def test_chunking_strategies_do_not_regress_on_the_golden_set():
    baseline = gates.load_baseline()
    measured = gates.chunking_metrics()
    assert set(baseline["chunking"]) == set(measured), "strategy set changed: refresh the baseline"
    failures: list[str] = []
    for strategy, expected in baseline["chunking"].items():
        failures += gates.regressions(measured[strategy], expected, baseline["tolerance"], f"chunking/{strategy}")
    assert not failures, "\n".join(failures)


async def test_the_whole_pipeline_does_not_regress_on_the_golden_set(pool):
    baseline = gates.load_baseline()
    assert set(baseline["pipeline"]) == set(gates.PIPELINE_MODES)
    failures: list[str] = []
    for mode in gates.PIPELINE_MODES:
        result = await run_pipeline_eval(pool, mode)
        assert result["documents_not_ready"] == 0, f"a golden document failed to ingest (KNOWLEDGE_CHUNKER={mode})"
        failures += gates.regressions(
            result, baseline["pipeline"][mode], baseline["tolerance"], f"pipeline/{mode}") + (
            [f"  missed: {result['missed']}"] if gates.regressions(
                result, baseline["pipeline"][mode], baseline["tolerance"], "") else [])
    assert not failures, "\n".join(failures)


def test_the_regression_check_itself_can_fail():
    base = {"recall_at_5": 0.8, "mrr": 0.7, "answer_in_context": 0.8}
    assert gates.regressions(base, base, 0.05, "x") == []
    assert gates.regressions({**base, "mrr": 0.66}, base, 0.05, "x") == []  # inside the tolerance
    assert len(gates.regressions({**base, "mrr": 0.6, "recall_at_5": 0.7}, base, 0.05, "x")) == 2


async def test_rerank_plumbing_reorders_and_reports_latency_on_the_golden_set(pool):
    """PLUMBING ONLY. A deterministic fake cross-encoder over the fake embedder proves the rerank flag and hook run
    through retrieve() end to end, change the order, and account their time. It says nothing about whether a real
    reranker improves real retrieval: that needs a real provider and real tenant questions."""
    from services.knowledge.tests.eval.fake_reranker import FakeCrossEncoderReranker
    reranker = FakeCrossEncoderReranker(latency_s=0.005)
    result = await run_pipeline_eval(pool, reranker=reranker)
    assert result["documents_not_ready"] == 0
    assert reranker.calls > 0 and "rerank_skipped" not in result["degraded"]
    assert result["reordered"] > 0, "the fake scorer never changed an order: the hook is not in the loop"
    assert min(result["rerank_ms"]) >= 4 and max(result["rerank_ms"]) < 120  # accounted, and inside the stage budget
    assert result["n"] == len(QUESTIONS)


async def test_enrichment_plumbing_runs_through_the_whole_pipeline_on_the_golden_set(pool, monkeypatch):
    """PLUMBING ONLY (see the rerank test above). A fake OpenAI-compatible LLM writes a sentence from the title in the
    prompt; the run proves the tenant's LLM config is found, chunks get enriched, every document still ends ready and
    the stored chunks stay the original text. Whether enrichment IMPROVES retrieval is not established: it needs a
    real embedder and a real LLM (docs/knowledge-phase7.md), and the feature ships off by default."""
    import json
    import re

    import httpx

    from services.knowledge import enrichment

    def llm(request):
        user = json.loads(request.content)["messages"][1]["content"]
        title = re.search(r"<title>(.*?)</title>", user).group(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": f"This passage is part of the {title} document."}}]})

    monkeypatch.setenv("KNOWLEDGE_ENRICH_CHUNKS", "1")
    monkeypatch.setenv("EVAL_FAKE_LLM_KEY", "k")
    monkeypatch.setattr(enrichment, "_test_transport", httpx.MockTransport(llm))
    result = await run_pipeline_eval(pool, llm_config={"engine": "openai", "model": "fake", "api_key_ref": "env:EVAL_FAKE_LLM_KEY"})
    assert result["documents_not_ready"] == 0
    assert result["enriched_chunks"] > 0 and result["enrich_failed"] == 0
    assert result["n"] == len(QUESTIONS)
