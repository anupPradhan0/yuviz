"""Parse limits. Env-tunable; every one has a plain rejection reason in parse_document()."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)
_WARNED_GARBAGE = False
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("", "0", "false", "no", "off")


def binary_parsers_enabled() -> bool:
    """THE one place KNOWLEDGE_BINARY_PARSERS is read (Limits, the parser entry point, the upload route and the worker
    all call this). PDF/Office parsing is OFF unless the operator sets it to 1/true/yes/on: it runs libraries that handle
    hostile files in a child that is not contained (docs/knowledge-parser-sandbox.md), so an operator opts in once the
    contained-worker deployment exists or the risk is accepted in writing. A value that is neither is OFF, with one warning."""
    global _WARNED_GARBAGE
    raw = os.environ.get("KNOWLEDGE_BINARY_PARSERS", "0").strip().lower()
    if raw in _TRUE:
        return True
    if raw not in _FALSE and not _WARNED_GARBAGE:
        _WARNED_GARBAGE = True
        log.warning("KNOWLEDGE_BINARY_PARSERS=%r is not one of 1/true/yes/on or 0/false/no/off; PDF/Office parsing stays OFF", raw[:40])
    return False


def _env(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 50 * 1024 * 1024
    max_pages: int = 500
    max_decompressed: int = 256 * 1024 * 1024   # zip members, summed, as actually inflated
    max_chars: int = 10_000_000                 # extracted text
    max_table_rows: int = 200_000
    max_cells: int = 2_000_000                  # table cells (rows x columns, padding included), checked before building
    max_columns: int = 2_000                    # per table row
    max_elements: int = 500_000                 # HTML elements (bs4 costs ~1.4 KB each: 3.9 MB of tags reached 795 MB)
    max_ooxml_elements: int = 2_500_000         # DOCX/PPTX XML elements (python-docx ~0.3-1 KB each; ~1,500 pages of tables)
                                                # XLSX is bounded by max_cells instead (its element cap is 3 x max_cells)
    max_paragraphs: int = 800_000               # DOCX/PPTX paragraph elements, counted by streaming before parsing
    pdf_page_bytes: int = 2 * 1024 * 1024       # decoded content stream of one PDF page (pypdf interprets ~0.2 MB/s); larger pages are skipped
    pdf_budget_s: float = 25.0                  # wall clock for PDF text extraction across pages; extraction stops there
    timeout_s: float = 60.0                     # wall clock for the whole parse subprocess
    memory_mb: int = 1024                       # RSS cap enforced by the parent's watchdog (all platforms)
    binary_parsers: bool = field(default_factory=binary_parsers_enabled)   # KNOWLEDGE_BINARY_PARSERS: False = no PDF/Office, HTML via pure-Python html.parser, no lxml
    keep_collapsed: bool = False                # HTML closed disclosures (details, collapsed panels) are dropped; 1 keeps them
    index_speaker_notes: bool = False           # PPTX notes are internal; off unless KNOWLEDGE_INDEX_SPEAKER_NOTES=1

    @classmethod
    def from_env(cls) -> "Limits":
        d = cls()
        return cls(
            max_bytes=int(_env("KB_PARSE_MAX_BYTES", d.max_bytes)),
            max_pages=int(_env("KB_PARSE_MAX_PAGES", d.max_pages)),
            max_decompressed=int(_env("KB_PARSE_MAX_DECOMPRESSED", d.max_decompressed)),
            max_chars=int(_env("KB_PARSE_MAX_CHARS", d.max_chars)),
            max_table_rows=int(_env("KB_PARSE_MAX_TABLE_ROWS", d.max_table_rows)),
            max_cells=int(_env("KB_PARSE_MAX_CELLS", d.max_cells)),
            max_columns=int(_env("KB_PARSE_MAX_COLUMNS", d.max_columns)),
            max_paragraphs=int(_env("KB_PARSE_MAX_PARAGRAPHS", d.max_paragraphs)),
            max_elements=int(_env("KB_PARSE_MAX_ELEMENTS", d.max_elements)),
            max_ooxml_elements=int(_env("KB_PARSE_MAX_OOXML_ELEMENTS", d.max_ooxml_elements)),
            pdf_page_bytes=int(_env("KB_PARSE_PDF_PAGE_BYTES", d.pdf_page_bytes)),
            pdf_budget_s=_env("KB_PARSE_PDF_BUDGET_S", d.pdf_budget_s),
            timeout_s=_env("KB_PARSE_TIMEOUT_S", d.timeout_s),
            memory_mb=int(_env("KB_PARSE_MEMORY_MB", d.memory_mb)),
            binary_parsers=binary_parsers_enabled(),
            keep_collapsed=os.environ.get("KNOWLEDGE_KEEP_COLLAPSED_CONTENT", "").lower() in ("1", "true", "yes"),
            index_speaker_notes=os.environ.get("KNOWLEDGE_INDEX_SPEAKER_NOTES", "").lower() in ("1", "true", "yes"),
        )
