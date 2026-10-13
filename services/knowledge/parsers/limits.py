"""Parse limits. Env-tunable; every one has a plain rejection reason in parse_document()."""

from __future__ import annotations

import os
from dataclasses import dataclass


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
    timeout_s: float = 60.0                     # wall clock for the whole parse subprocess
    memory_mb: int = 1024                       # RSS cap enforced by the parent's watchdog (all platforms)
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
            timeout_s=_env("KB_PARSE_TIMEOUT_S", d.timeout_s),
            memory_mb=int(_env("KB_PARSE_MEMORY_MB", d.memory_mb)),
            index_speaker_notes=os.environ.get("KNOWLEDGE_INDEX_SPEAKER_NOTES", "").lower() in ("1", "true", "yes"),
        )
