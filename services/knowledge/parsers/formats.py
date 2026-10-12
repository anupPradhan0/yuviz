"""Per-type fallback ladders. Runs inside the sandboxed subprocess (see sandbox.py), never in the worker itself.

Each ladder is an ordered list of (parser_name, callable); run_ladder() takes the first result that passes the
quality gate, else the best soft-failing one as degraded, else raises ParseRejected with the most specific reason."""

from __future__ import annotations

import csv
import io
import re
import zipfile
import zlib
from typing import Callable

from lxml import etree

from . import detect, ocr
from .limits import Limits
from .model import CODE, HEADING, LIST, PARAGRAPH, TABLE, Block, ParsedDocument, ParseRejected
from .quality import MIN_CHARS_PER_PAGE, assess
from .textblocks import blocks_from_text

_XML = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
_CORRUPT = "file is corrupt or not a valid {kind}"


class TierSkip(Exception):
    """This fallback does not apply to the document (e.g. OCR when every page already has text)."""


def run_ladder(tiers: list[tuple[str, Callable[[], ParsedDocument]]]) -> ParsedDocument:
    best: ParsedDocument | None = None
    best_score = -1.0
    last_reject: ParseRejected | None = None
    last_hard: str | None = None
    for name, fn in tiers:
        try:
            doc = fn()
        except TierSkip:
            continue
        except ParseRejected as exc:
            last_reject = exc
            continue
        except Exception:  # a parser crashing on odd input is exactly why there is a next rung
            continue
        doc.parser_used = name
        q = assess(doc.text(), doc.page_texts)
        doc.quality_score = q.score
        if q.hard_fail:
            last_hard = q.hard_fail
            continue
        if q.ok and not doc.degraded:
            return doc
        doc.degraded = True
        doc.warnings = [*doc.warnings, *q.soft_issues]
        if q.score > best_score:
            best, best_score = doc, q.score
    if best is not None:
        if last_reject is not None and last_reject.reason == ocr.NEEDS_OCR:
            best.warnings.append(ocr.NEEDS_OCR)
        return best
    if last_reject is not None:
        raise last_reject
    raise ParseRejected(f"{last_hard or 'no text could be extracted'}; the file may be empty, scanned or damaged")


# ── zip-based Office files ──────────────────────────────────────────────

def guard_zip(raw: bytes, limits: Limits, kind: str) -> zipfile.ZipFile:
    """Open the archive and inflate every member for real (bounded), so a header that lies about its size or a
    decompression bomb is refused before any parser touches it."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        infos = zf.infolist()
        if any(i.flag_bits & 0x1 for i in infos):
            raise ParseRejected("document is password-protected")
        if sum(i.file_size for i in infos) > limits.max_decompressed:
            raise ParseRejected("file expands beyond the allowed size (possible decompression bomb)")
        total = 0
        for info in infos:
            with zf.open(info) as member:
                while chunk := member.read(1 << 20):
                    total += len(chunk)
                    if total > limits.max_decompressed:
                        raise ParseRejected("file expands beyond the allowed size (possible decompression bomb)")
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, OSError) as exc:
        raise ParseRejected(_CORRUPT.format(kind=kind)) from exc
    return zf


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text(el) -> str:
    parts: list[str] = []
    for node in el.iter():
        name = _local(node.tag) if isinstance(node.tag, str) else ""
        if name in ("t", "delText") and node.text:
            parts.append(node.text)
        elif name in ("tab",):
            parts.append(" ")
        elif name in ("br", "cr"):
            parts.append("\n")
    return "".join(parts).strip()


# ── DOCX ────────────────────────────────────────────────────────────────

def _docx_native(raw: bytes) -> ParsedDocument:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(io.BytesIO(raw))
    blocks: list[Block] = []
    items: list[str] = []

    def flush() -> None:
        if items:
            blocks.append(Block(LIST, "\n".join(items)))
            items.clear()

    for child in d.element.body.iterchildren():
        name = _local(child.tag)
        if name == "p":
            p = Paragraph(child, d)
            text = p.text.strip()
            if not text:
                continue
            try:
                style = (p.style.name or "") if p.style is not None else ""
            except Exception:
                style = ""
            m = re.match(r"Heading\s+(\d)", style)
            is_numbered = child.find(".//{*}numPr") is not None
            if m or style == "Title":
                flush()
                blocks.append(Block(HEADING, text, level=int(m.group(1)) if m else 1))
            elif style.startswith("List") or is_numbered:
                items.append(f"- {text}")
            else:
                flush()
                blocks.append(Block(PARAGRAPH, text))
        elif name == "tbl":
            flush()
            rows = tuple(tuple(c.text.strip() for c in row.cells) for row in Table(child, d).rows)
            if any(any(c for c in r) for r in rows):
                blocks.append(Block(TABLE, rows=rows))
    flush()
    return ParsedDocument(blocks, detect.DOCX)


def _docx_xml(raw: bytes, limits: Limits) -> ParsedDocument:
    zf = guard_zip(raw, limits, "DOCX")
    root = etree.fromstring(zf.read("word/document.xml"), _XML)
    body = root.find("{*}body")
    blocks: list[Block] = []
    for child in body if body is not None else []:
        name = _local(child.tag)
        if name == "p":
            text = _xml_text(child)
            if text:
                blocks.append(Block(PARAGRAPH, text))
        elif name == "tbl":
            rows = tuple(
                tuple(_xml_text(tc) for tc in tr.findall("{*}tc")) for tr in child.iter("{*}tr")
            )
            if any(any(c for c in r) for r in rows):
                blocks.append(Block(TABLE, rows=rows))
    return ParsedDocument(blocks, detect.DOCX)


# ── PPTX ────────────────────────────────────────────────────────────────

def _pptx_native(raw: bytes, limits: Limits) -> ParsedDocument:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(raw))
    if len(prs.slides) > limits.max_pages:
        raise ParseRejected(f"presentation has more than {limits.max_pages} slides")
    blocks: list[Block] = []
    for n, slide in enumerate(prs.slides, start=1):
        title_shape = slide.shapes.title
        title = title_shape.text_frame.text.strip() if title_shape is not None and title_shape.has_text_frame else ""
        blocks.append(Block(HEADING, title or f"Slide {n}", level=1, page=n))
        for shape in slide.shapes:
            if shape is title_shape:
                continue
            if shape.has_text_frame:
                lines = [p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip()]
                if lines:
                    blocks.append(Block(PARAGRAPH, "\n".join(lines), page=n))
            elif getattr(shape, "has_table", False) and shape.has_table:
                rows = tuple(tuple(c.text.strip() for c in r.cells) for r in shape.table.rows)
                blocks.append(Block(TABLE, rows=rows, page=n))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip() if slide.notes_slide.notes_text_frame else ""
            if notes:
                blocks.append(Block(PARAGRAPH, f"Speaker notes: {notes}", page=n))
    return ParsedDocument(blocks, detect.PPTX, pages=len(prs.slides))


def _pptx_xml(raw: bytes, limits: Limits) -> ParsedDocument:
    zf = guard_zip(raw, limits, "PPTX")
    slides = sorted(
        (n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
        key=lambda n: int(re.search(r"(\d+)\.xml", n).group(1)),
    )
    blocks = []
    for i, name in enumerate(slides[: limits.max_pages], start=1):
        root = etree.fromstring(zf.read(name), _XML)
        paras = [t for t in (_xml_text(p) for p in root.iter("{*}p")) if t]
        if paras:
            blocks.append(Block(HEADING, f"Slide {i}", level=1, page=i))
            blocks.append(Block(PARAGRAPH, "\n".join(paras), page=i))
    return ParsedDocument(blocks, detect.PPTX)


# ── XLSX ────────────────────────────────────────────────────────────────

def _xlsx_native(raw: bytes, limits: Limits) -> ParsedDocument:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    blocks: list[Block] = []
    budget = limits.max_table_rows
    try:
        for ws in wb.worksheets:
            rows: list[tuple[str, ...]] = []
            for row in ws.iter_rows(values_only=True):
                cells = tuple("" if v is None else str(v).strip() for v in row)
                if any(cells):
                    rows.append(cells)
                    budget -= 1
                    if budget <= 0:
                        raise ParseRejected(f"spreadsheet has more than {limits.max_table_rows} rows")
            if rows:
                width = max(len(r) for r in rows)
                blocks.append(Block(HEADING, ws.title, level=1))
                blocks.append(Block(TABLE, rows=tuple(r + ("",) * (width - len(r)) for r in rows)))
    finally:
        wb.close()
    return ParsedDocument(blocks, detect.XLSX)


def _xlsx_xml(raw: bytes, limits: Limits) -> ParsedDocument:
    zf = guard_zip(raw, limits, "XLSX")
    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        root = etree.fromstring(zf.read("xl/sharedStrings.xml"), _XML)
        shared = [_xml_text(si) for si in root.iter("{*}si")]
    blocks: list[Block] = []
    for name in sorted(n for n in zf.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
        root = etree.fromstring(zf.read(name), _XML)
        rows = []
        for r in root.iter("{*}row"):
            cells = []
            for c in r.findall("{*}c"):
                v = c.find("{*}v")
                val = "" if v is None or v.text is None else v.text
                if c.get("t") == "s" and val.isdigit() and int(val) < len(shared):
                    val = shared[int(val)]
                elif c.get("t") == "inlineStr":
                    val = _xml_text(c)
                cells.append(val.strip())
            if any(cells):
                rows.append(tuple(cells))
            if len(rows) > limits.max_table_rows:
                raise ParseRejected(f"spreadsheet has more than {limits.max_table_rows} rows")
        if rows:
            width = max(len(r) for r in rows)
            blocks.append(Block(HEADING, name.rsplit("/", 1)[-1][:-4], level=1))
            blocks.append(Block(TABLE, rows=tuple(r + ("",) * (width - len(r)) for r in rows)))
    return ParsedDocument(blocks, detect.XLSX)


# ── HTML ────────────────────────────────────────────────────────────────

_BOILERPLATE_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "form", "nav", "header", "footer",
                     "aside", "button", "select", "dialog")
_BOILERPLATE_ATTR_RE = re.compile(
    r"(^|[\s_-])(nav|navbar|menu|footer|sidebar|cookie|consent|banner|advert|ads?|breadcrumb|share|social|popup|modal)([\s_-]|$)",
    re.IGNORECASE,
)


def _html_main(html: str) -> ParsedDocument:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(list(_BOILERPLATE_TAGS)):
        tag.decompose()
    for tag in soup.find_all(attrs={"class": _BOILERPLATE_ATTR_RE}) + soup.find_all(attrs={"id": _BOILERPLATE_ATTR_RE}):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"}) or soup.body or soup
    blocks: list[Block] = []
    items: list[str] = []

    def flush() -> None:
        if items:
            blocks.append(Block(LIST, "\n".join(items)))
            items.clear()

    def walk(node) -> None:
        for child in node.children:
            name = getattr(child, "name", None)
            if name is None:
                continue
            if re.fullmatch(r"h[1-6]", name):
                flush()
                text = child.get_text(" ", strip=True)
                if text:
                    blocks.append(Block(HEADING, text, level=int(name[1])))
            elif name in ("ul", "ol"):
                flush()
                lis = [f"- {li.get_text(' ', strip=True)}" for li in child.find_all("li") if li.get_text(strip=True)]
                if lis:
                    blocks.append(Block(LIST, "\n".join(lis)))
            elif name == "table":
                flush()
                rows = tuple(
                    tuple(c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"]))
                    for tr in child.find_all("tr")
                )
                rows = tuple(r for r in rows if any(r))
                if rows:
                    width = max(len(r) for r in rows)
                    blocks.append(Block(TABLE, rows=tuple(r + ("",) * (width - len(r)) for r in rows)))
            elif name == "pre":
                flush()
                blocks.append(Block(CODE, child.get_text()))
            elif name in ("p", "blockquote", "figcaption", "dd", "dt", "summary"):
                flush()
                text = child.get_text(" ", strip=True)
                if text:
                    blocks.append(Block(PARAGRAPH, text))
            elif name == "li":
                items.append(f"- {child.get_text(' ', strip=True)}")
            elif any(child.find(t) for t in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "pre")):
                walk(child)
            else:
                text = child.get_text(" ", strip=True)
                if text:
                    flush()
                    blocks.append(Block(PARAGRAPH, text))

    walk(root)
    flush()
    return ParsedDocument(blocks, detect.HTML)


def _html_strip(html: str) -> ParsedDocument:
    text = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1\s*>", " ", html)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|br|section|article)\s*>|<br\s*/?>", "\n\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    import html as html_mod
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return ParsedDocument(blocks_from_text(re.sub(r"\n\s*\n+", "\n\n", text)), detect.HTML)


# ── CSV / TSV, plain text ───────────────────────────────────────────────

def _csv_rows(text: str, delimiter: str | None, limits: Limits) -> ParsedDocument:
    sample = text[:50_000]
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    csv.field_size_limit(1 << 24)
    rows: list[tuple[str, ...]] = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        cells = tuple(c.strip() for c in row)
        if any(cells):
            rows.append(cells)
        if len(rows) > limits.max_table_rows:
            raise ParseRejected(f"table has more than {limits.max_table_rows} rows")
    if not rows:
        raise ParseRejected("file is empty")
    width = max(len(r) for r in rows)
    rows = [r + ("",) * (width - len(r)) for r in rows]
    return ParsedDocument([Block(TABLE, rows=tuple(rows))], detect.CSV)


def _text_blocks(text: str, kind: str, guess_headings: bool = False) -> ParsedDocument:
    return ParsedDocument(blocks_from_text(text, guess_headings=guess_headings), kind)


# ── PDF ─────────────────────────────────────────────────────────────────

def _open_pdf(raw: bytes, limits: Limits):
    """The reader, or None when pypdf cannot open it (the raw rung may still recover text).
    Encryption and page-count rejections are definitive, so they are raised here and not retried."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:
                ok = 0
            if not ok:
                raise ParseRejected("document is password-protected")
        n = len(reader.pages)
    except ParseRejected:
        raise
    except Exception:
        if re.search(rb"/Encrypt\b", raw):
            raise ParseRejected("document is password-protected")
        return None
    if n > limits.max_pages:
        raise ParseRejected(f"document has more than {limits.max_pages} pages")
    return reader


def _pdf_pages_to_doc(page_texts: list[str], degraded: bool = False, warnings: list[str] | None = None) -> ParsedDocument:
    blocks: list[Block] = []
    for n, text in enumerate(page_texts, start=1):
        blocks.extend(blocks_from_text(text, page=n, guess_headings=True))
    return ParsedDocument(blocks, detect.PDF, pages=len(page_texts), page_texts=page_texts,
                          degraded=degraded, warnings=warnings or [])


def _pdf_pypdf(reader, state: dict) -> ParsedDocument:
    if reader is None:
        raise TierSkip
    texts = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            texts.append("")
    state["pypdf_had_text"] = any(t.strip() for t in texts)
    return _pdf_pages_to_doc(texts)


_STR_OP = re.compile(rb"\(((?:\\.|[^\\()])*)\)")
_TJ_OP = re.compile(rb"\[((?:\\.|[^\]])*)\]\s*TJ|\(((?:\\.|[^\\()])*)\)\s*(?:Tj|')", re.DOTALL)
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}
_MAX_STREAM = 20 * 1024 * 1024


def _unescape(s: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(s):
        if s[i:i + 1] == b"\\" and i + 1 < len(s):
            nxt = s[i + 1:i + 2]
            if nxt in _ESCAPES:
                out += _ESCAPES[nxt]
                i += 2
                continue
            m = re.match(rb"[0-7]{1,3}", s[i + 1:])
            if m:
                out.append(int(m.group(), 8) & 0xFF)
                i += 1 + len(m.group())
                continue
            i += 1
            continue
        out.append(s[i])
        i += 1
    return bytes(out)


def _pdf_raw(raw: bytes, state: dict) -> ParsedDocument:
    """Text-layer rung for files pypdf cannot open or read nothing from: inflate content streams and read the
    show-text operators. Handles simple (non-CID) fonts only; the quality gate rejects what comes out garbled.
    Skipped when pypdf did read text: this rung has no page boundaries, so it would hide empty pages."""
    if state.get("pypdf_had_text"):
        raise TierSkip
    lines: list[str] = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.DOTALL):
        data = m.group(1)
        try:
            d = zlib.decompressobj()
            data = d.decompress(data, _MAX_STREAM)
        except zlib.error:
            pass
        if b"Tj" not in data and b"TJ" not in data:
            continue
        for op in _TJ_OP.finditer(data):
            if op.group(1) is not None:
                parts = [_unescape(s) for s in _STR_OP.findall(op.group(1))]
                chunk = b"".join(parts)
            else:
                chunk = _unescape(op.group(2))
            text = chunk.decode("latin-1").strip()
            if text:
                lines.append(text)
    if not lines:
        raise TierSkip
    return ParsedDocument(blocks_from_text("\n".join(lines), guess_headings=True), detect.PDF)


def _pdf_ocr(reader, language: str | None) -> ParsedDocument:
    """OCR the pages that have no text layer, using the images embedded in them (a scan is one image per page)."""
    if reader is None:
        raise TierSkip
    texts: list[str] = []
    empty: list[int] = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        texts.append(t)
        if len(t.strip()) < MIN_CHARS_PER_PAGE:
            empty.append(i)
    if not empty:
        raise TierSkip
    if not ocr.ocr_available():
        raise ParseRejected(ocr.NEEDS_OCR)
    failed = 0
    for i in empty:
        try:
            found = [ocr.ocr_image(img.data, language) for img in reader.pages[i].images]
            text = "\n".join(t.strip() for t in found if t.strip())
        except Exception:
            text = ""
        if text:
            texts[i] = text
        else:
            failed += 1
    warnings = [f"{failed} of {len(texts)} pages could not be read by OCR"] if failed else []
    return _pdf_pages_to_doc(texts, degraded=bool(failed), warnings=warnings)


# ── entry point ─────────────────────────────────────────────────────────

def parse_by_type(raw: bytes, content_type: str, limits: Limits, language: str | None = None) -> ParsedDocument:
    ct = content_type
    if ct == detect.PDF:
        reader = _open_pdf(raw, limits)
        state: dict = {}
        doc = run_ladder([
            ("pypdf", lambda: _pdf_pypdf(reader, state)),
            ("pdf-raw", lambda: _pdf_raw(raw, state)),
            ("ocr", lambda: _pdf_ocr(reader, language)),
        ])
    elif ct == detect.DOCX:
        guard_zip(raw, limits, "DOCX")
        doc = run_ladder([("python-docx", lambda: _docx_native(raw)), ("docx-xml", lambda: _docx_xml(raw, limits))])
    elif ct == detect.PPTX:
        guard_zip(raw, limits, "PPTX")
        doc = run_ladder([("python-pptx", lambda: _pptx_native(raw, limits)), ("pptx-xml", lambda: _pptx_xml(raw, limits))])
    elif ct == detect.XLSX:
        guard_zip(raw, limits, "XLSX")
        doc = run_ladder([("openpyxl", lambda: _xlsx_native(raw, limits)), ("xlsx-xml", lambda: _xlsx_xml(raw, limits))])
    else:
        decoded = detect.decode_text(raw)
        if decoded is None:
            raise ParseRejected("file is not readable text")
        text, encoding = decoded
        text = text.lstrip("﻿")
        if ct == detect.HTML:
            doc = run_ladder([("bs4-main", lambda: _html_main(text)), ("html-strip", lambda: _html_strip(text))])
        elif ct in (detect.CSV, detect.TSV):
            delim = "\t" if ct == detect.TSV else None
            doc = run_ladder([("csv", lambda: _csv_rows(text, delim, limits)), ("text", lambda: _text_blocks(text, ct))])
        else:
            doc = run_ladder([("text", lambda: _text_blocks(text, ct))])
        if encoding not in ("utf-8", "ascii"):
            doc.warnings.append(f"decoded as {encoding}")
    doc.detected_type = ct
    if len(doc.text()) > limits.max_chars:
        raise ParseRejected("extracted text exceeds the allowed size")
    return doc
