"""Phase 3 parsers: detection from bytes, fallback ladders, limits, quality gate, OCR adapter, sandbox.
No database needed; each parse runs in the real sandbox subprocess."""

from __future__ import annotations

import os
import stat
import sys

import pytest

from services.knowledge.parsers import Limits, ParseRejected, detect_content_type, parse_document
from services.knowledge.parsers import detect, ocr
from services.knowledge.parsers import formats
from services.knowledge.parsers.formats import TierSkip, run_ladder
from services.knowledge.parsers.model import PARAGRAPH, Block, ParsedDocument
from services.knowledge.parsers.quality import assess, garbage_ratio
from services.knowledge.tests.corpus import builders as B

SMALL_BOMB_LIMIT = Limits(max_decompressed=2 * 1024 * 1024)


async def _reason(raw: bytes, **kw) -> str:
    with pytest.raises(ParseRejected) as e:
        await parse_document(raw, **kw)
    return e.value.reason


# ── detection from bytes ────────────────────────────────────────────────

def test_detection_uses_bytes_not_declared_type_or_extension():
    assert detect_content_type(B.text_pdf(), hint="text/plain") == detect.PDF
    assert detect_content_type(B.docx_with_tables(), hint="application/pdf") == detect.DOCX
    assert detect_content_type(B.pptx_deck()) == detect.PPTX
    assert detect_content_type(B.xlsx_sheet()) == detect.XLSX
    assert detect_content_type(B.HTML_PAGE, hint="text/plain") == detect.HTML
    assert detect_content_type(B.CSV_DATA, hint="text/plain") == detect.CSV
    assert detect_content_type(b"a\tb\tc\n1\t2\t3\n4\t5\t6\n") == detect.TSV
    assert detect_content_type(b"\x89PNG\r\n\x1a\n" + bytes(50), hint="text/plain") == "image/png"
    assert detect_content_type(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(50)) == detect.OLE
    assert detect_content_type(b"") == detect.EMPTY
    assert detect_content_type(bytes(range(256)) * 4) == detect.BINARY
    assert detect_content_type("Hello there, this is plain prose.".encode()) == detect.PLAIN
    assert detect_content_type(b"# Title\n\nbody text", hint=None) == detect.MARKDOWN


def test_prose_with_commas_is_not_mistaken_for_csv():
    prose = b"Hello, world.\nThis is a sentence, with one comma.\nAnother line without any.\nAnd a, last, one.\n"
    assert detect_content_type(prose) == detect.PLAIN


# ── rejections with a plain reason ──────────────────────────────────────

async def test_empty_and_whitespace_only_are_rejected():
    assert await _reason(b"") == "file is empty"
    assert "no text" in await _reason(b" \n\t\n ")


async def test_oversized_is_rejected_before_parsing():
    assert "larger than" in await _reason(b"x" * 2048, limits=Limits(max_bytes=1024))


async def test_page_limit_is_enforced():
    pdf = B.make_pdf([{"text": f"Page number {i} has enough text to count."} for i in range(5)])
    assert "more than 3 pages" in await _reason(pdf, limits=Limits(max_pages=3))


async def test_encrypted_pdf_is_password_protected():
    assert await _reason(B.encrypted_pdf()) == "document is password-protected"


async def test_corrupt_pdf_and_unsupported_formats_get_reasons():
    assert "no text" in await _reason(B.corrupt_pdf())
    assert "legacy or password-protected" in await _reason(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(100))
    assert "not supported" in await _reason(b"\x89PNG\r\n\x1a\n" + bytes(100))


async def test_corrupt_zip_office_file_is_rejected():
    assert "corrupt" in await _reason(b"PK\x03\x04" + bytes(range(200)))
    broken = B.docx_with_tables()
    assert "corrupt" in await _reason(broken[: len(broken) // 2])


# ── zip bomb guard ──────────────────────────────────────────────────────

async def test_zip_bomb_trips_the_decompression_limit():
    bomb = B.zip_bomb(8 * 1024 * 1024)
    assert len(bomb) < 100 * 1024  # tiny on disk, 8 MB inflated: safe to build and to refuse
    assert "decompression bomb" in await _reason(bomb, limits=SMALL_BOMB_LIMIT)


async def test_same_archive_parses_when_under_the_limit():
    doc = await parse_document(B.zip_bomb(1024 * 1024), limits=Limits(max_decompressed=8 * 1024 * 1024))
    assert doc.detected_type == detect.DOCX


async def test_tampered_zip_header_is_refused_as_corrupt():
    assert await _reason(B.lying_zip_bomb(), limits=SMALL_BOMB_LIMIT) == "file is corrupt or not a valid DOCX"


# ── structure is preserved ──────────────────────────────────────────────

async def test_docx_keeps_headings_and_tables():
    doc = await parse_document(B.docx_with_tables())
    assert doc.parser_used == "python-docx"
    heads = [(b.level, b.text) for b in doc.blocks if b.kind == "heading"]
    assert heads == [(1, "Pricing Handbook"), (2, "Plans"), (2, "Cancellation")]
    (table,) = [b for b in doc.blocks if b.kind == "table"]
    assert table.rows[0] == ("Plan", "Seats", "Price") and table.rows[2] == ("Team", "25", "$99")


async def test_docx_xml_strip_fallback_reads_the_same_content():
    import io, zipfile
    from services.knowledge.parsers.formats import _docx_xml
    doc = _docx_xml(zipfile.ZipFile(io.BytesIO(B.docx_with_tables())))
    assert any(b.kind == "table" and b.rows[3][0] == "Enterprise" for b in doc.blocks)
    assert "Cancel plan" in doc.text()


async def test_pptx_and_xlsx():
    deck = await parse_document(B.pptx_deck())
    assert deck.parser_used == "python-pptx" and "Onboarding" in deck.text() and deck.blocks[0].page == 1
    sheet = await parse_document(B.xlsx_sheet())
    (table,) = [b for b in sheet.blocks if b.kind == "table"]
    assert table.rows[0] == ("City", "Delivery days", "Fee") and len(table.rows) == 5


async def test_html_extracts_main_content_without_boilerplate():
    doc = await parse_document(B.HTML_PAGE)
    text = doc.text()
    assert doc.parser_used == "bs4-main"
    assert "return any item within 30 days" in text and "# Returns" in text and "## How to return" in text
    assert "Electronics | 15 days" in text
    for noise in ("LOGIN-NAV-LINK", "COOKIE-BANNER-TEXT", "FOOTER-BOILERPLATE", "DO-NOT-INDEX-TRACKER"):
        assert noise not in text


async def test_html_has_no_tag_stripping_fallback_that_would_leak_hidden_text():
    # one rung only: a page the main parser rejects is rejected, never downgraded to a CSS-blind parser
    assert not hasattr(formats, "_html_strip")


async def test_csv_becomes_one_table_with_header_first():
    doc = await parse_document(B.CSV_DATA)
    (table,) = doc.blocks
    assert table.kind == "table" and table.rows[0] == ("sku", "product", "price", "stock") and len(table.rows) == 121


async def test_pdf_records_page_numbers_and_headings():
    doc = await parse_document(B.text_pdf())
    assert {b.page for b in doc.blocks} == {1, 2}
    assert ("heading", "SHIPPING") in [(b.kind, b.text) for b in doc.blocks]
    assert doc.pages == 2 and doc.quality_score == 1.0 and not doc.degraded


# ── encodings ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["utf16_bom.txt", "utf16_le.txt", "utf8_bom.md", "latin1.txt", "mixed_encoding.txt"])
async def test_mixed_encodings_decode_to_real_text(name):
    doc = await parse_document(B.build_corpus()[name].data)
    text = doc.text()
    assert "�" not in text and len(text) > 100
    if name == "latin1.txt":
        assert "Señor García" in text and "crème brûlée" in text


async def test_rtl_text_survives_intact():
    doc = await parse_document(B.ARABIC.encode())
    assert "سياسة الاسترداد" in doc.text()
    doc = await parse_document(B.HEBREW.encode())
    assert "מדיניות החזרים" in doc.text()


# ── quality gate and the ladder ─────────────────────────────────────────

def test_quality_gate_flags_garbage_and_empty_pages():
    assert garbage_ratio("hello world") == 0.0
    assert garbage_ratio("��� ok") > 0.4
    assert assess("�" * 50 + " text").hard_fail == "extracted text is garbled"
    assert assess("   ").hard_fail == "no text could be extracted"
    q = assess("x" * 200, pages=["x" * 200, "", ""])
    assert q.hard_fail is None and not q.ok and "2 of 3 pages" in q.soft_issues[0] and q.score < 0.6
    assert assess("x" * 200, pages=["x" * 100, "y" * 100]).ok


def _doc(text: str, pages: list[str] | None = None) -> ParsedDocument:
    return ParsedDocument([Block(PARAGRAPH, text)], page_texts=pages)


def test_failing_quality_gate_drops_to_the_next_fallback():
    calls: list[str] = []

    def garbled():
        calls.append("first")
        return _doc("�" * 100 + "ab")

    def good():
        calls.append("second")
        return _doc("A perfectly readable paragraph of text.")

    doc = run_ladder([("first", garbled), ("second", good)])
    assert calls == ["first", "second"] and doc.parser_used == "second" and doc.quality_score == 1.0 and not doc.degraded


def test_soft_failure_is_kept_as_degraded_and_best_score_wins():
    thin = lambda: _doc("x" * 100, pages=["x" * 100, ""])           # noqa: E731
    thinner = lambda: _doc("x" * 100, pages=["x" * 100, "", "", ""])  # noqa: E731
    doc = run_ladder([("thinner", thinner), ("thin", thin), ("skip", lambda: (_ for _ in ()).throw(TierSkip()))])
    assert doc.degraded and doc.parser_used == "thin" and doc.quality_score > 0


def test_ladder_with_nothing_usable_raises_the_most_specific_reason():
    def needs_ocr():
        raise ParseRejected(ocr.NEEDS_OCR)
    with pytest.raises(ParseRejected) as e:
        run_ladder([("empty", lambda: _doc("  ")), ("ocr", needs_ocr)])
    assert e.value.reason == ocr.NEEDS_OCR


async def test_truncated_pdf_is_recovered_by_the_raw_fallback():
    doc = await parse_document(B.truncated_pdf())
    assert doc.parser_used == "pdf-raw" and "refund" in doc.text().lower()


# ── OCR adapter ─────────────────────────────────────────────────────────

def _no_ocr(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # no tesseract; sys.executable is absolute so the sandbox still starts


async def test_scanned_pdf_without_ocr_engine_fails_with_the_clear_reason(monkeypatch, tmp_path):
    _no_ocr(monkeypatch, tmp_path)
    assert not ocr.ocr_available()
    assert await _reason(B.scanned_pdf()) == "scanned PDF needs OCR; no OCR engine available"


async def test_partly_scanned_pdf_without_ocr_is_degraded_not_failed(monkeypatch, tmp_path):
    _no_ocr(monkeypatch, tmp_path)
    doc = await parse_document(B.half_scanned_pdf())
    assert doc.degraded and "1 of 2 pages" in doc.warnings[0] and ocr.NEEDS_OCR in doc.warnings
    assert "Customers may request a refund" in doc.text()


def _fake_tesseract(tmp_path, body: str) -> str:
    path = tmp_path / "tesseract"
    path.write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.buffer.read()\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(tmp_path)


async def test_scanned_pdf_is_read_through_the_ocr_adapter_when_the_binary_exists(monkeypatch, tmp_path):
    bindir = _fake_tesseract(tmp_path, "print('Scanned page: refunds are processed within 30 days of return.')")
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
    doc = await parse_document(B.scanned_pdf())
    assert doc.parser_used == "ocr" and not doc.degraded
    assert "refunds are processed within 30 days" in doc.text()


async def test_ocr_engine_failure_on_every_page_is_still_a_defined_outcome(monkeypatch, tmp_path):
    bindir = _fake_tesseract(tmp_path, "sys.exit(3)")
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
    with pytest.raises(ParseRejected):
        await parse_document(B.scanned_pdf())
    doc = await parse_document(B.half_scanned_pdf())
    assert doc.degraded and "1 of 2 pages" in " ".join(doc.warnings)


# ── sandbox ─────────────────────────────────────────────────────────────

async def test_parse_timeout_kills_the_subprocess_and_gives_a_reason():
    assert "timed out" in await _reason(B.docx_with_tables(), limits=Limits(timeout_s=0.01))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_AS is applied on Linux only (UNVERIFIED on macOS)")
async def test_address_space_limit_does_not_break_a_normal_parse_on_linux():
    doc = await parse_document(B.docx_with_tables(), limits=Limits(memory_mb=1024))
    assert doc.parser_used == "python-docx"
