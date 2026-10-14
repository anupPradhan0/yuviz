"""Knob sweep for the chunking default. Run: python -m services.knowledge.tests.eval.sweep  (prints a markdown table).

Every row is one configuration indexed over the whole golden corpus (ALL_DOCS, one index per document kind, see
harness.evaluate) with the hash embedder. Structure and recursive rows return the PARENT section when a child matches,
as production will; `ctx tok` is the average number of estimated tokens returned per query (top 3 contexts)."""

from __future__ import annotations

from services.knowledge.chunker import ChunkConfig
from services.knowledge.tests.eval.golden import ALL_DOCS, QUESTIONS
from services.knowledge.tests.eval.harness import (
    Report, Strategy, evaluate, fixed_window_strategy, mode_strategy, recursive_strategy, structure_strategy,
)


def _cfg(child: int = 250, **kw) -> ChunkConfig:
    return ChunkConfig(cap=max(512, child + 128), child_target=child, **kw)


def candidates() -> dict[str, Strategy]:
    c: dict[str, Strategy] = {}
    for size, overlap in ((200, 40), (120, 20), (300, 60), (200, 0), (400, 80)):
        c[f"fixed {size}w/{overlap}"] = fixed_window_strategy(size, overlap)
    for child in (150, 250, 400, 600):
        c[f"recursive child={child}"] = recursive_strategy(_cfg(child))
    for child in (150, 250, 400, 600):
        for crumb in (True, False):
            c[f"structure child={child} crumb={'on' if crumb else 'off'}"] = structure_strategy(_cfg(child, breadcrumb=crumb))
    for pack in (125, 250, 500):
        c[f"structure pack={pack}"] = structure_strategy(_cfg(250, pack_target=pack))
    for parent in (600, 900, 1400):
        c[f"structure parent_max={parent}"] = structure_strategy(_cfg(250, parent_max=parent))
    c["structure child-only (no parent)"] = structure_strategy(_cfg(250), return_parent=False)
    c["recursive child-only (no parent)"] = recursive_strategy(_cfg(250), return_parent=False)
    for mode in ("auto", "structure", "recursive", "fixed"):
        c[f"KNOWLEDGE_CHUNKER={mode}"] = mode_strategy(mode, _cfg(250))
    return c


def run() -> Report:
    return evaluate(ALL_DOCS, QUESTIONS, strategies=candidates())


KINDS = ("policy", "prose", "faq", "table")


def markdown(r: Report) -> str:
    head = "| strategy | " + " | ".join(f"{k} R@5 / MRR / ctx / tok" for k in KINDS) + " | mixed KB R@5 / MRR |"
    lines = [head, "|" + "---|" * (len(KINDS) + 2)]
    for s in r.by_strategy:
        cells = []
        for k in KINDS:
            m = r.by_strategy_and_kind[(s, k)]
            cells.append(f"{m.recall_at_5:.2f} / {m.mrr:.2f} / {m.answer_in_context:.2f} / {m.avg_context_tokens:.0f}")
        m = r.by_strategy[s]
        lines.append(f"| {s} | " + " | ".join(cells) + f" | {m.recall_at_5:.2f} / {m.mrr:.2f} |")
    return "\n".join(lines)


if __name__ == "__main__":
    from services.knowledge.tests.eval.golden import QUESTIONS as Q
    print(f"questions={len(Q)} documents={len(ALL_DOCS)}")
    print(markdown(run()))
