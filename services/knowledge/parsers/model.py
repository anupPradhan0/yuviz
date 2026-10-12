"""What a parser hands the chunker: an ordered list of structural blocks, plus how well the parse went."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

HEADING, PARAGRAPH, LIST, TABLE, CODE = "heading", "paragraph", "list", "table", "code"


class ParseRejected(ValueError):
    """The document cannot be ingested; `reason` is plain and safe to show to the tenant.
    A ValueError, so the worker classifies it as permanent (retrying the same bytes cannot help)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LimitExceeded(ParseRejected):
    """A resource limit (cells, columns, rows, characters) was hit. Final: the ladder does not try a cheaper parser,
    because the document is too big for the platform, not unreadable."""


class ParseInfrastructureError(RuntimeError):
    """The parser could not run (killed by a signal, import failure, no output): not the document's fault, so
    a RuntimeError, which the worker classifies as transient and retries."""


@dataclass(frozen=True)
class Block:
    kind: str
    text: str = ""                        # heading/paragraph text, list items one per line, code body
    level: int = 0                        # heading level 1-6
    page: int | None = None               # PDF page / slide number
    rows: tuple[tuple[str, ...], ...] = ()  # table rows; rows[0] is the header

    def render(self) -> str:
        """Markdown-ish text of the block; this is what gets embedded and shown."""
        if self.kind == HEADING:
            return f"{'#' * max(1, min(self.level, 6))} {self.text}"
        if self.kind == TABLE:
            return "\n".join(render_row(r) for r in self.rows)
        return self.text


def render_row(row: tuple[str, ...] | list[str]) -> str:
    return "| " + " | ".join(c.replace("\n", " ").replace("|", "/") for c in row) + " |"


@dataclass
class ParsedDocument:
    blocks: list[Block]
    detected_type: str = "text/plain"
    parser_used: str = ""
    quality_score: float = 1.0
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)
    pages: int | None = None
    page_texts: list[str] | None = None   # per-page text, for the quality gate (not sent back from the sandbox)
    hidden_removed: dict[str, int] = field(default_factory=dict)  # invisible content dropped, by kind

    def text(self) -> str:
        return "\n\n".join(b.render() for b in self.blocks if b.render().strip())

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ParsedDocument":
        blocks = [
            Block(kind=b["kind"], text=b["text"], level=b["level"], page=b["page"],
                  rows=tuple(tuple(r) for r in b["rows"]))
            for b in data["blocks"]
        ]
        return cls(**{**data, "blocks": blocks})
