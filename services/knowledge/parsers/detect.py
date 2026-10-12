"""Content-type detection from the file's bytes. The client-declared type and the filename are never consulted
(a hint is accepted only to break a tie between text flavours that bytes cannot separate)."""

from __future__ import annotations

import io
import re
import zipfile

import charset_normalizer

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HTML, CSV, TSV = "text/html", "text/csv", "text/tab-separated-values"
MARKDOWN, PLAIN = "text/markdown", "text/plain"

SUPPORTED_CONTENT_TYPES = frozenset({PDF, DOCX, PPTX, XLSX, HTML, CSV, TSV, MARKDOWN, PLAIN})

EMPTY = "application/x-empty"
OLE = "application/x-ole-storage"      # legacy .doc/.xls/.ppt, and password-protected OOXML
ZIP = "application/zip"
BINARY = "application/octet-stream"

_IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF8", "image/gif"),
                (b"II*\x00", "image/tiff"), (b"MM\x00*", "image/tiff"))
_OTHER_BINARY_MAGIC = (b"\x1f\x8b", b"\x7fELF", b"MZ", b"BZh", b"7z\xbc\xaf", b"Rar!", b"\xfd7zXZ")
_BOMS = ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
         (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"))
_TRUSTED_LEGACY = frozenset({
    "gb18030", "big5", "shift_jis", "cp932", "euc_jp", "euc_kr", "cp949", "iso2022_jp", "iso2022_kr", "hz",
    "cp1251", "koi8_r", "koi8_u", "cp1256", "cp1255", "cp1253", "cp874",
})
_HTML_RE = re.compile(
    r"^\s*(<!doctype\s+html|<html|<head|<body|<(div|p|h[1-6]|table|ul|ol|article|section|main|span|nav|header)[\s>])",
    re.IGNORECASE,
)
_MD_RE = re.compile(r"^(#{1,6}\s+\S|\s*[-*]\s+\S.*\n\s*[-*]\s+\S|```)", re.MULTILINE)


def decode_text(raw: bytes) -> tuple[str, str] | None:
    """(text, encoding) or None when the bytes are binary. BOM first, then strict UTF-8, then a detector,
    then cp1252/latin-1 (which never fails, so mixed or legacy encodings still yield text)."""
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            try:
                return raw.decode(enc), enc
            except UnicodeDecodeError:
                break
    head = raw[:8192]
    if b"\x00" in head:
        # BOM-less UTF-16: NULs alternate with ASCII bytes. Anything else with NULs is binary.
        nul_even, nul_odd = head[0::2].count(0), head[1::2].count(0)
        half = max(1, len(head) // 2)
        for enc, nuls in (("utf-16-le", nul_odd), ("utf-16-be", nul_even)):
            if nuls / half > 0.3:
                try:
                    return raw.decode(enc), enc
                except UnicodeDecodeError:
                    pass
        return None
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    controls = sum(1 for b in head if b < 9 or 13 < b < 32)
    if controls / max(1, len(head)) > 0.05:
        return None
    # The detector confuses the Latin single-byte code pages (a latin-1 "señor" comes back as cp1250), so only its
    # answers for multi-byte and non-Latin legacy encodings are trusted; everything else is cp1252.
    best = charset_normalizer.from_bytes(raw[:200_000]).best()
    if best is not None and best.encoding in _TRUSTED_LEGACY:
        try:
            return raw.decode(best.encoding), best.encoding
        except (UnicodeDecodeError, LookupError):
            pass
    return raw.decode("cp1252", errors="replace"), "cp1252"


def _zip_kind(raw: bytes) -> str:
    try:
        names = set(zipfile.ZipFile(io.BytesIO(raw)).namelist())
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError):
        return ZIP
    if "word/document.xml" in names:
        return DOCX
    if "ppt/presentation.xml" in names:
        return PPTX
    if "xl/workbook.xml" in names:
        return XLSX
    return ZIP


def _delimited(text: str) -> str | None:
    """csv/tsv when >= 3 non-empty lines all carry the same delimiter count (>= 2 for , ; |, >= 1 for tab)."""
    lines = [ln for ln in text.splitlines()[:60] if ln.strip()]
    if len(lines) < 3:
        return None
    for delim, minimum, kind in (("\t", 1, TSV), (",", 2, CSV), (";", 2, CSV), ("|", 2, CSV)):
        counts = [ln.count(delim) for ln in lines]
        if counts[0] >= minimum and sum(c == counts[0] for c in counts) / len(counts) >= 0.9:
            if max(len(ln) for ln in lines) < 4000:
                return kind
    return None


def detect_content_type(raw: bytes, hint: str | None = None) -> str:
    if not raw:
        return EMPTY
    # The header must open the file (after a BOM or whitespace): a markdown note that merely mentions "%PDF-" is text.
    if raw[:64].lstrip(b"\xef\xbb\xbf \t\r\n\x00").startswith(b"%PDF-"):
        return PDF
    if raw[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return _zip_kind(raw)
    if raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return OLE
    for magic, mime in _IMAGE_MAGIC:
        if raw.startswith(magic):
            return mime
    if raw.startswith(_OTHER_BINARY_MAGIC):
        return BINARY
    decoded = decode_text(raw)
    if decoded is None:
        return BINARY
    text = decoded[0].lstrip("﻿")
    if _HTML_RE.match(text[:2048]):
        return HTML
    delimited = _delimited(text)
    if delimited:
        return delimited
    if hint == MARKDOWN or _MD_RE.search(text[:20000]):
        return MARKDOWN
    return PLAIN
