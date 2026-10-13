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
