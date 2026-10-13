"""Runs the bake-off on the golden set. Hash embedder only: see harness.py for what that does and does not show."""

from __future__ import annotations

import pytest

from services.knowledge.tests.eval import sweep
from services.knowledge.tests.eval.golden import ALL_DOCS, DOCS, QUESTIONS
from services.knowledge.tests.eval.harness import STRATEGIES, evaluate, format_report, hash_embed


def test_golden_set_is_big_enough_and_answers_exist_in_their_documents():
    assert len(QUESTIONS) >= 30
    import re
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()  # noqa: E731
    for q in QUESTIONS:
        assert norm(q.answer) in norm(DOCS[q.doc][1]).replace("\n", " "), q.question
        assert all(norm(t) in norm(DOCS[q.doc][1]) for t in q.terms), q.question


def test_hash_embedder_is_deterministic_and_normalised():
    a, b = hash_embed("refund within 30 days"), hash_embed("refund within 30 days")
    assert a == b and abs(sum(x * x for x in a) - 1.0) < 1e-9
    assert sum(x * y for x, y in zip(a, hash_embed("refund within 30 days"))) > 0.99
    assert sum(x * y for x, y in zip(a, hash_embed("completely unrelated shipping words"))) < 0.5


# Measured with the hash embedder (see docs/knowledge-chunking.md for the whole sweep). Policy documents, 24 questions
# (one question = 0.042), one index per document kind, production-style context (parent section when a child matches):
#   fixed 200w/40         recall@5 0.92  MRR 0.79  in-context 0.83  ~854 tokens returned per query
#   structure (default)   recall@5 0.79  MRR 0.70  in-context 0.75  ~543 tokens
#   best structure config recall@5 0.88  MRR 0.77  in-context 0.83  ~1255 tokens (child=600): still not ahead
# Nothing in the sweep beat fixed windows on headed or plain prose, so KNOWLEDGE_CHUNKER=auto uses fixed windows there.
# These tests pin the numbers with tolerances and pin the DECISION: if a structure/recursive configuration ever beats
# fixed windows on policy documents, the decision test fails and the default has to be revisited on purpose.
TOL = 0.05


@pytest.fixture(scope="module")
def sweep_report():
    return sweep.run()


def _m(report, name, kind):
    return report.by_strategy_and_kind[(name, kind)]


def test_sweep_numbers_are_pinned_within_tolerance(sweep_report):
    fixed = _m(sweep_report, "fixed 200w/40", "policy")
    assert fixed.n == 24
    assert fixed.recall_at_5 == pytest.approx(0.92, abs=TOL) and fixed.mrr == pytest.approx(0.79, abs=TOL)
    assert fixed.answer_in_context == pytest.approx(0.83, abs=TOL) and fixed.avg_context_tokens == pytest.approx(854, rel=0.1)
    structure = _m(sweep_report, "structure child=250 crumb=on", "policy")
    assert structure.recall_at_5 == pytest.approx(0.79, abs=TOL) and structure.mrr == pytest.approx(0.70, abs=TOL)
    assert structure.answer_in_context == pytest.approx(0.75, abs=TOL) and structure.avg_context_tokens == pytest.approx(543, rel=0.1)


def test_no_structure_or_recursive_configuration_beats_fixed_windows_on_policy_documents(sweep_report):
    """The data behind the default. A configuration 'beats' the baseline when it is better on recall@5 AND MRR by more
    than one question. If this fails, a more complex strategy has started to win: revisit KNOWLEDGE_CHUNKER=auto."""
    fixed = _m(sweep_report, "fixed 200w/40", "policy")
    contenders = [n for n in sweep_report.by_strategy if n.startswith(("structure ", "recursive "))]
    assert len(contenders) >= 15
    winners = [n for n in contenders
               if _m(sweep_report, n, "policy").recall_at_5 > fixed.recall_at_5 + 0.042
               and _m(sweep_report, n, "policy").mrr > fixed.mrr + 0.042]
    assert winners == []


def test_production_style_context_is_larger_than_the_matched_chunk(sweep_report):
    parent = _m(sweep_report, "structure child=250 crumb=on", "prose")
    child = _m(sweep_report, "structure child-only (no parent)", "prose")
    assert parent.avg_context_tokens > child.avg_context_tokens and parent.answer_in_context >= child.answer_in_context


def test_auto_default_equals_fixed_on_prose_and_policy_and_uses_routes_for_qa(sweep_report):
    auto = "KNOWLEDGE_CHUNKER=auto"
    for kind in ("policy", "prose"):
        a, f = _m(sweep_report, auto, kind), _m(sweep_report, "KNOWLEDGE_CHUNKER=fixed", kind)
        assert (a.recall_at_5, a.mrr, a.answer_in_context) == pytest.approx((f.recall_at_5, f.mrr, f.answer_in_context))
    qa, fx = _m(sweep_report, auto, "faq"), _m(sweep_report, "KNOWLEDGE_CHUNKER=fixed", "faq")
    # Q&A: same accuracy, a fraction of the returned context. Only 5 questions and fixed windows also score 1.00, so
    # this is a context-size win, not an accuracy win.
    assert qa.recall_at_5 >= fx.recall_at_5 and qa.answer_in_context >= fx.answer_in_context
    assert qa.avg_context_tokens < 0.4 * fx.avg_context_tokens


def test_table_documents_are_saturated_so_the_table_route_is_a_design_choice_not_a_measured_win(sweep_report):
    for mode in ("auto", "fixed"):
        assert _m(sweep_report, f"KNOWLEDGE_CHUNKER={mode}", "table").recall_at_5 == 1.0  # cannot tell them apart


def test_sweep_table_renders_every_row():
    text = sweep.markdown(sweep.run())
    assert text.count("\n") >= 30 and "KNOWLEDGE_CHUNKER=auto" in text


def test_winner_is_the_best_strategy_and_a_broken_strategy_never_wins():
    from services.knowledge.tests.eval.harness import IndexedChunk, STRATEGIES

    def first_words_only(name, kind, text):  # indexes just the first 5 words of each document
        head = " ".join(text.split()[:5])
        return [IndexedChunk(name, head, head)]

    strategies = {**STRATEGIES, "broken": first_words_only}
    report = evaluate(ALL_DOCS, QUESTIONS, strategies=strategies)
    for kind in ("policy", "faq", "table", "prose"):
        best = max(m.mrr for (s, k), m in report.by_strategy_and_kind.items() if k == kind)
        w = report.winner(kind)
        assert report.by_strategy_and_kind[(w, kind)].mrr == best and w != "broken"
        assert report.by_strategy_and_kind[("broken", kind)].mrr < best
    assert report.by_strategy["broken"].recall_at_5 < 0.3 < report.by_strategy["fixed-window"].recall_at_5
