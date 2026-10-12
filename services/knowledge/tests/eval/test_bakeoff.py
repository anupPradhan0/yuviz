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


# Measured gap, hash embedder, 24 policy questions against ~30 policy documents (one question = 0.042):
#   fixed-window  recall@5 0.92  MRR 0.79  answer-in-context 0.83
#   structure     recall@5 0.79  MRR 0.70  answer-in-context 0.75
# Structure-aware is WORSE than fixed-window in this harness, mostly because it produces ~50% more chunks (108 vs
# 71) and a lexical embedder ranks small, similar chunks poorly. The test pins the gap so a regression shows up and
# an improvement forces this comment to be updated; it does not claim structure-aware wins.
RECALL_GAP_TOLERANCE = 0.15
MRR_GAP_TOLERANCE = 0.12


def test_structure_aware_gap_to_fixed_window_on_the_policy_corpus_stays_within_the_measured_margin(capsys):
    report = evaluate(ALL_DOCS, QUESTIONS)
    with capsys.disabled():
        print("\n" + format_report(report))
    fixed = report.by_strategy_and_kind[("fixed-window", "policy")]
    structure = report.by_strategy_and_kind[("structure", "policy")]
    assert structure.n == fixed.n >= 20
    assert structure.recall_at_5 >= fixed.recall_at_5 - RECALL_GAP_TOLERANCE
    assert structure.mrr >= fixed.mrr - MRR_GAP_TOLERANCE
    assert structure.recall_at_5 >= 0.75 and structure.mrr >= 0.65
    parent = report.by_strategy_and_kind[("structure+parent", "policy")]
    assert parent.answer_in_context >= structure.answer_in_context


def test_winner_is_the_best_strategy_and_a_broken_strategy_never_wins():
    from services.knowledge.tests.eval.harness import IndexedChunk, STRATEGIES

    def first_words_only(name, kind, text):  # indexes just the first 5 words of each document
        head = " ".join(text.split()[:5])
        return [IndexedChunk(name, head, head)]

    strategies = {**STRATEGIES, "broken": first_words_only}
    report = evaluate(ALL_DOCS, QUESTIONS, strategies=strategies)
    for kind in ("policy", "faq", "table", "prose"):
        best = max(m.mrr for (s, k), m in report.by_strategy_and_kind.items() if k == kind)
        w = max(strategies, key=lambda s: (report.by_strategy_and_kind[(s, kind)].mrr, report.by_strategy_and_kind[(s, kind)].recall_at_5))
        assert report.by_strategy_and_kind[(w, kind)].mrr == best and w != "broken"
        assert report.by_strategy_and_kind[("broken", kind)].mrr < best
    assert report.by_strategy["broken"].recall_at_5 < 0.3 < report.by_strategy["fixed-window"].recall_at_5
