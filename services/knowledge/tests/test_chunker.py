"""Phase 4: structure-aware chunker, hard token cap, breadcrumbs, parent-child, routing. No database."""

from __future__ import annotations

import random

import pytest

from services.knowledge import chunker
from services.knowledge.chunker import ChunkConfig, chunk_document, chunk_plain_text, route
from services.knowledge.chunking import chunk_text
from services.knowledge.parsers.model import PARAGRAPH, TABLE, Block
from services.knowledge.parsers.textblocks import blocks_from_text
from services.knowledge.tokens import EstimatingTokenCounter

COUNTER = EstimatingTokenCounter()
CAP = ChunkConfig().cap

HINDI = "रिफंड नीति: ग्राहक खरीद की तारीख से तीस दिनों के भीतर पूरा पैसा वापस मांग सकते हैं। पैसा मूल भुगतान माध्यम में लौटाया जाता है। " * 80
ARABIC = "سياسة الاسترداد: يمكن للعملاء طلب استرداد كامل المبلغ خلال ثلاثين يوما من تاريخ الشراء. يتم إرجاع الأموال إلى وسيلة الدفع الأصلية. " * 80
THAI = "นโยบายการคืนเงิน ลูกค้าสามารถขอคืนเงินเต็มจำนวนได้ภายในสามสิบวันนับจากวันที่ซื้อสินค้า เงินจะถูกคืนไปยังช่องทางการชำระเงินเดิม" * 80
CHINESE = "退款政策：客户可以在购买之日起三十天内申请全额退款。退款将退回到原来的付款方式。" * 120
CODE = "\n".join(
    f"def handler_{i}(request, *args, **kwargs):\n    value = request.get('key_{i}', {{}})['nested'][0]\n"
    f"    return {{'id': {i}, 'ok': value is not None, 'items': [x for x in value if x]}}\n" for i in range(150)
)
NO_BREAKS = "A1b2C3d4" * 3000  # one 24,000-character "word": no whitespace or punctuation to split on
ENGLISH = "Refunds are processed within thirty days of the original purchase date. Items must be returned unused. " * 100


def _assert_within_cap(result, cap=CAP, counter=COUNTER):
    assert result.children
    for c in result.children:
        assert c.content.strip()
        assert counter.count(c.content) <= cap, (counter.count(c.content), c.content[:60])
        assert c.token_count == counter.count(c.content)


# ── hard token cap ──────────────────────────────────────────────────────

@pytest.mark.parametrize("name,text", [
    ("hindi", HINDI), ("arabic", ARABIC), ("thai", THAI), ("chinese", CHINESE), ("code", CODE),
    ("english", ENGLISH), ("no-breaks", NO_BREAKS),
])
@pytest.mark.parametrize("cap", [512, 256, 128])
def test_no_chunk_exceeds_the_cap_in_any_script(name, text, cap):
    result = chunk_document(blocks_from_text(text), config=ChunkConfig.for_embedder(cap))
    _assert_within_cap(result, cap)
    # An independent, stricter-than-any-tokenizer bound: a wordpiece tokenizer emits at most one token per character,
    # so for non-Latin text the character count must also fit the cap (spaces excluded).
    if name in ("hindi", "arabic", "thai", "chinese"):
        for c in result.children:
            assert len("".join(c.content.split())) <= cap


def test_hard_cap_holds_even_when_the_splitter_cannot_find_a_boundary():
    # Mutation guard: with the cap enforcement removed this single 24k-char token becomes one huge chunk.
    result = chunk_document([Block(PARAGRAPH, NO_BREAKS)], config=ChunkConfig.for_embedder(128))
    assert len(result.children) > 20
    _assert_within_cap(result, 128)


def test_oversized_table_and_list_are_split_to_the_cap_not_emitted_whole():
    rows = tuple(("SKU%04d" % i, "A fairly long product description " * 3, str(i)) for i in range(200))
    result = chunk_document([Block(TABLE, rows=(("sku", "description", "n"),) + rows)], config=ChunkConfig())
    _assert_within_cap(result)
    assert len(result.children) > 3


def test_estimator_is_conservative_for_english_and_scripts():
    # ~1.3 real tokens per English word; the estimate must not be lower.
    assert COUNTER.count("the quick brown fox jumps over the lazy dog") >= 12
    assert COUNTER.count(HINDI[:200]) >= len(HINDI[:200].replace(" ", "")) - 5  # about one per character


def test_token_counter_is_pluggable():
    class Words:
        def count(self, text):
            return len(text.split()) * 3

    r = chunk_document(blocks_from_text(ENGLISH), counter=Words(), config=ChunkConfig(cap=200, child_target=100))
    assert all(len(c.content.split()) * 3 <= 200 for c in r.children)


# ── structure: breadcrumbs, tables, lists ───────────────────────────────

POLICY = """# Store policy

## Refunds

### Eligibility

Items can be refunded within 30 days of delivery if unused and in original packaging. Digital goods are not refundable once downloaded.

### Process

1. Open the order page and choose Return item.
2. Print the prepaid label and ship the parcel.
3. The refund is issued five business days after the parcel arrives.

## Shipping

| Region | Days | Fee |
| --- | --- | --- |
| Domestic | 3-5 | Free over $50 |
| Europe | 7-10 | $12 |
| Rest of world | 10-14 | $25 |

Orders placed after 2pm ship the next business day.
"""


def _big_section(topic: str) -> str:
    return " ".join(f"Marker{topic} sentence number {i} explains how the step works for customers in great detail." for i in range(40))


def test_headings_split_chunks_and_prefix_a_breadcrumb():
    doc = f"# Handbook\n\n## Refunds\n\n### Eligibility\n\n{_big_section('Alpha')}\n\n### Process\n\n{_big_section('Bravo')}\n\n## Shipping\n\n{_big_section('Charlie')}\n"
    r = chunk_plain_text(doc)
    assert r.strategy == "structure"
    paths = {c.section_path for c in r.children}
    assert {"Handbook > Refunds > Eligibility", "Handbook > Refunds > Process", "Handbook > Shipping"} <= paths
    for c in r.children:
        assert c.content.startswith(c.section_path + "\n\n")
        mine = {"Eligibility": "Alpha", "Process": "Bravo", "Shipping": "Charlie"}[c.section_path.rsplit(" > ", 1)[-1]]
        assert all(f"Marker{o}" not in c.content for o in ("Alpha", "Bravo", "Charlie") if o != mine), c.section_path


def test_short_sibling_sections_are_packed_into_one_chunk_with_their_headings_inline():
    r = chunk_plain_text(POLICY)
    refunds = next(c for c in r.children if "Print the prepaid label" in c.content)
    assert refunds.section_path == "Store policy > Refunds"
    assert "## Eligibility" in refunds.content and "## Process" in refunds.content and "refunded within 30 days" in refunds.content


def test_lists_and_tables_are_kept_whole():
    r = chunk_plain_text(POLICY)
    process = next(c for c in r.children if "Print the prepaid label" in c.content)
    assert all(f"{n}." in process.content for n in "123")
    shipping = next(c for c in r.children if "| Domestic |" in c.content)
    assert "| Rest of world | 10-14 | $25 |" in shipping.content and "| Region | Days | Fee |" in shipping.content


def test_small_sections_fold_into_the_next_one_instead_of_becoming_tiny_chunks():
    r = chunk_plain_text(POLICY)
    assert all(c.token_count >= 20 for c in r.children)
    assert any("Store policy" in c.content for c in r.children)


# ── routing ─────────────────────────────────────────────────────────────

QA = "\n\n".join(f"Q: How do I do thing number {i}?\nA: To do thing {i}, open settings and choose option {i}." for i in range(6))


def test_qa_documents_get_one_pair_per_chunk():
    r = chunk_plain_text(QA)
    assert r.strategy == "qa" and len(r.children) == 6
    for i, c in enumerate(r.children):
        assert f"thing number {i}?" in c.content and f"choose option {i}." in c.content
        assert c.content.count("Q:") == 1 and c.content.count("A:") == 1


def test_csv_style_tables_chunk_into_row_groups_with_the_header_repeated():
    rows = (("sku", "product", "price"),) + tuple((f"SKU{i:04d}", f"Widget number {i}", f"{i}.99") for i in range(300))
    r = chunk_document([Block(TABLE, rows=rows)])
    assert r.strategy == "table" and len(r.children) > 2 and not r.parents
    for c in r.children:
        assert c.content.startswith("| sku | product | price |")
    body = "\n".join(c.content for c in r.children)
    assert all(f"SKU{i:04d}" in body for i in range(300))  # no row lost between groups


def test_unstructured_prose_uses_the_recursive_splitter():
    r = chunk_plain_text(ENGLISH)
    assert r.strategy == "prose" and len(r.children) > 3
    _assert_within_cap(r)
    assert all(c.section_path is None for c in r.children)


def test_route_choices():
    assert route(blocks_from_text(QA)) == "qa"
    assert route(blocks_from_text(POLICY)) == "structure"
    assert route(blocks_from_text(ENGLISH)) == "prose"
    assert route([Block(TABLE, rows=(("a", "b"), ("1", "2")))]) == "table"


def test_any_splitter_failure_falls_back_to_fixed_windows(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("splitter bug")

    monkeypatch.setattr(chunker, "_build_sections", boom)
    r = chunk_plain_text(POLICY)
    assert r.strategy == "fixed" and r.children
    _assert_within_cap(r)
    monkeypatch.setattr(chunker, "_build_qa", boom)
    assert chunk_plain_text(QA).strategy == "fixed"


def test_empty_input_gives_no_chunks():
    assert chunk_document([]).children == []
    assert chunk_plain_text("  \n\n ").children == []


def test_legacy_chunk_text_is_unchanged():
    chunks = chunk_text("Refunds take 30 days to process.", chunk_size=200)
    assert len(chunks) == 1 and chunks[0].token_count == 6 and chunks[0].parent is None and not chunks[0].is_parent


# ── parent-child ────────────────────────────────────────────────────────

def _long_sectioned_doc() -> str:
    rng = random.Random(7)
    words = "refund shipping order parcel label customer policy window delivery support account billing".split()
    para = lambda: " ".join(rng.choice(words) for _ in range(90)) + "."  # noqa: E731
    return "# Handbook\n\n## Returns\n\n" + "\n\n".join(para() for _ in range(14)) + "\n\n## Billing\n\nShort billing note about invoices and receipts for customers.\n"


def test_long_sections_get_parents_and_children_point_at_them():
    r = chunk_plain_text(_long_sectioned_doc())
    assert r.parents and r.children
    linked = [c for c in r.children if c.parent is not None]
    assert linked and all(0 <= c.parent < len(r.parents) for c in linked)
    for c in linked:
        parent = r.parents[c.parent]
        assert parent.is_parent and parent.section_path == c.section_path
        assert c.content.split("\n\n", 1)[1] in parent.content  # the parent holds the child's text
        assert parent.token_count > c.token_count
    assert all(p.token_count <= ChunkConfig().parent_max + 40 for p in r.parents)
    assert all(not c.is_parent for c in r.children)
    # the short section needs no parent: its single child is returned as it is
    billing = next(c for c in r.children if c.section_path.endswith("Billing"))
    assert billing.parent is None


def test_a_lone_child_gets_no_parent_row():
    r = chunk_plain_text("# Title\n\nOne short paragraph that fits in a single chunk easily and stays alone here.\n\n"
                         "## Next\n\nAnother short paragraph with enough words to avoid folding into its neighbour okay.\n")
    assert r.parents == []
