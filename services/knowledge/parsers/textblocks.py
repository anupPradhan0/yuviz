"""Plain/markdown text -> blocks: headings, pipe tables, lists, fenced code, paragraphs."""

from __future__ import annotations

import re

from .model import CODE, HEADING, LIST, PARAGRAPH, TABLE, Block

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+•▪●]|\d{1,3}[.)])\s+\S")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# PDF/plain-text headings that carry no markup: "1. Scope", "2.3 Refund policy", or SHORT ALL-CAPS LINES.
_NUMBERED_HEADING_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})[.)]?\s+([^\W\d_][^\n]{2,70})$")


def _split_row(line: str) -> tuple[str, ...]:
    return tuple(c.strip() for c in line.strip().strip("|").split("|"))


def blocks_from_text(text: str, page: int | None = None, guess_headings: bool = False) -> list[Block]:
    blocks: list[Block] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i, n = 0, len(lines)
    para: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append(Block(PARAGRAPH, "\n".join(para), page=page))
            para.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush_para()
            i += 1
            continue
        if stripped.startswith("```"):
            flush_para()
            j = i + 1
            while j < n and not lines[j].strip().startswith("```"):
                j += 1
            blocks.append(Block(CODE, "\n".join(lines[i + 1:j]), page=page))
            i = j + 1
            continue
        m = _HEADING_RE.match(stripped)
        if m:
            flush_para()
            blocks.append(Block(HEADING, m.group(2), level=len(m.group(1)), page=page))
            i += 1
            continue
        if guess_headings and not para:
            nm = _NUMBERED_HEADING_RE.match(stripped)
            is_caps = stripped.isupper() and 3 <= len(stripped) <= 70 and len(stripped.split()) <= 8
            nxt = lines[i + 1].strip() if i + 1 < n else ""
            if (nm or is_caps) and nxt and not stripped.endswith((".", ":", ",", ";")):
                level = (nm.group(1).count(".") + 1) if nm else 1
                blocks.append(Block(HEADING, stripped, level=min(level, 6), page=page))
                i += 1
                continue
        if _TABLE_ROW_RE.match(line):
            flush_para()
            rows = []
            while i < n and _TABLE_ROW_RE.match(lines[i]):
                if not _TABLE_SEP_RE.match(lines[i]):
                    rows.append(_split_row(lines[i]))
                i += 1
            blocks.append(Block(TABLE, rows=tuple(rows), page=page))
            continue
        if _LIST_RE.match(line):
            flush_para()
            items: list[str] = []
            while i < n and lines[i].strip() and not _HEADING_RE.match(lines[i].strip()):
                if _LIST_RE.match(lines[i]):
                    items.append(lines[i].strip())
                elif items and lines[i].startswith((" ", "\t")):
                    items[-1] += " " + lines[i].strip()  # wrapped continuation of the previous item
                else:
                    break
                i += 1
            blocks.append(Block(LIST, "\n".join(items), page=page))
            continue
        para.append(line.rstrip())
        i += 1
    flush_para()
    return blocks
