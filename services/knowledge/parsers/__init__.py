"""Document parsing: detect the type from bytes, parse in a sandboxed subprocess down a fallback ladder,
gate on quality. parse_document() either returns a ParsedDocument or raises ParseRejected(reason)."""

from __future__ import annotations

from .detect import EMPTY, SUPPORTED_CONTENT_TYPES, detect_content_type
from .limits import Limits
from .model import Block, ParsedDocument, ParseRejected
from .sandbox import run_parse

_UNSUPPORTED_REASONS = {
    "application/x-ole-storage": "legacy or password-protected Office file; save it as an unprotected DOCX, XLSX or PPTX",
    "application/zip": "archive is not a DOCX, XLSX or PPTX file (or it is corrupt)",
}


def unsupported_reason(detected: str) -> str:
    if detected.startswith("image/"):
        return "image files are not supported; upload a PDF or text version"
    return _UNSUPPORTED_REASONS.get(detected, f"unsupported file format ({detected})")


async def parse_document(
    raw: bytes, hint: str | None = None, language: str | None = None, limits: Limits | None = None,
) -> ParsedDocument:
    limits = limits or Limits.from_env()
    if not raw:
        raise ParseRejected("file is empty")
    if len(raw) > limits.max_bytes:
        raise ParseRejected(f"file is larger than the {limits.max_bytes // (1024 * 1024)} MB limit")
    detected = detect_content_type(raw, hint)
    if detected not in SUPPORTED_CONTENT_TYPES:
        raise ParseRejected(unsupported_reason(detected))
    return await run_parse(raw, detected, limits, language)


__all__ = ["Block", "EMPTY", "Limits", "ParsedDocument", "ParseRejected", "SUPPORTED_CONTENT_TYPES",
           "detect_content_type", "parse_document", "unsupported_reason"]
