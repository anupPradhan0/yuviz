"""Document parsing: detect the type from bytes, parse in a sandboxed subprocess down a fallback ladder,
gate on quality. parse_document() either returns a ParsedDocument or raises ParseRejected(reason)."""

from __future__ import annotations

import dataclasses
import os

from .detect import DOCX, EMPTY, PDF, PPTX, SUPPORTED_CONTENT_TYPES, XLSX, detect_content_type
from .limits import Limits, binary_parsers_enabled
from .model import Block, ParsedDocument, ParseRejected
from .sandbox import run_parse

_UNSUPPORTED_REASONS = {
    "application/x-ole-storage": "legacy or password-protected Office file; save it as an unprotected DOCX, XLSX or PPTX",
    "application/zip": "archive is not a DOCX, XLSX or PPTX file (or it is corrupt)",
}


# PDF and Office parsing is where the complex, exploitable libraries are. it is OFF by default; KNOWLEDGE_BINARY_PARSERS=1 turns it on (text,
# markdown, HTML and CSV always work) until parsing runs in a separate, contained service (docs/knowledge-parser-sandbox.md).
BINARY_TYPES = frozenset({PDF, DOCX, PPTX, XLSX})
from .formats import BINARY_DISABLED_REASON  # noqa: E402  (one string, defined where the child can also use it)


def unsupported_reason(detected: str) -> str:
    if detected.startswith("image/"):
        return "image files are not supported; upload a PDF or text version"
    return _UNSUPPORTED_REASONS.get(detected, f"unsupported file format ({detected})")


async def parse_document(
    raw: bytes, hint: str | None = None, language: str | None = None, limits: Limits | None = None,
) -> ParsedDocument:
    limits = limits or Limits.from_env()
    if limits.binary_parsers and not binary_parsers_enabled():
        limits = dataclasses.replace(limits, binary_parsers=False)
    if not raw:
        raise ParseRejected("file is empty")
    if len(raw) > limits.max_bytes:
        raise ParseRejected(f"file is larger than the {limits.max_bytes // (1024 * 1024)} MB limit")
    detected = detect_content_type(raw, hint)
    if detected not in SUPPORTED_CONTENT_TYPES:
        raise ParseRejected(unsupported_reason(detected))
    if detected in BINARY_TYPES and not binary_parsers_enabled():
        raise ParseRejected(BINARY_DISABLED_REASON)
    return await run_parse(raw, detected, limits, language)


__all__ = ["BINARY_DISABLED_REASON", "BINARY_TYPES", "Block", "binary_parsers_enabled", "EMPTY", "Limits", "ParsedDocument", "ParseRejected", "SUPPORTED_CONTENT_TYPES",
           "detect_content_type", "parse_document", "unsupported_reason"]
