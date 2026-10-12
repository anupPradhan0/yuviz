"""Runs the bake-off on the golden set. Hash embedder only: see harness.py for what that does and does not show."""

from __future__ import annotations

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


def test_structure_aware_does_not_do_worse_than_fixed_window_on_the_policy_corpus(capsys):
    """Measured with the hash embedder on 21 policy questions against ~30 policy documents. One question is worth
    1/21 = 0.048, so "not worse" allows a single question of noise on the rank metrics and none on answer-in-context.
    At the time of writing fixed-window is ahead by exactly that margin on recall@5 (0.90 vs 0.86) and equal on
    answer-in-context: this harness does NOT show structure-aware winning, only that it is not clearly worse."""
    report = evaluate(ALL_DOCS, QUESTIONS)
    with capsys.disabled():
        print("\n" + format_report(report))
    fixed = report.by_strategy_and_kind[("fixed-window", "policy")]
    structure = report.by_strategy_and_kind[("structure", "policy")]
    assert structure.n == fixed.n >= 20
    one_question = 1 / structure.n + 1e-9
    assert structure.recall_at_5 >= fixed.recall_at_5 - one_question
    assert structure.mrr >= fixed.mrr - one_question
    assert structure.answer_in_context >= fixed.answer_in_context
    parent = report.by_strategy_and_kind[("structure+parent", "policy")]
    assert parent.answer_in_context >= fixed.answer_in_context


def test_report_covers_every_strategy_and_kind_and_names_a_winner():
    report = evaluate(ALL_DOCS, QUESTIONS)
    assert set(report.by_strategy) == set(STRATEGIES)
    for kind in ("policy", "faq", "table", "prose"):
        assert report.winner(kind) in STRATEGIES
