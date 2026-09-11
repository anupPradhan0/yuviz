"""Streaming-safe [[END_CALL]] / [[TRANSFER]] parsing (StreamBuffer + DirectiveParser)."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Union

_TAG_RE  = re.compile(r'\[\[(?P<name>[A-Z_]+)(?P<attrs>[^\]]*)\]\]')
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

# Strip markdown chars TTS would speak literally; per-chunk so split ** is fine.
_MARKDOWN_CHARS_RE = re.compile(r"[*_`#]")


def strip_markdown_chars(text: str) -> str:
    """Strip markdown chars TTS would speak; idempotent (shared TTS boundary)."""
    return _MARKDOWN_CHARS_RE.sub("", text)


class TransferType(str, Enum):
    """warm | cold | none — mirrors agents.transfer_type CHECK."""
    WARM = "warm"
    COLD = "cold"
    NONE = "none"


def coerce_transfer_type(raw: str) -> TransferType:
    """RuntimeConfig.policies.transfer_type (see libs.config_sdk) is a
    plain str sourced from the DB's CHECK-constrained column, so this
    should never actually miss — but a value list mismatch (however
    unlikely, e.g. a stale legacy-path config) degrades to NONE rather
    than raising, matching this module's existing "malformed input is
    tolerated, not fatal" posture (see DirectiveParser)."""
    try:
        return TransferType(raw)
    except ValueError:
        return TransferType.NONE


@dataclass(frozen=True)
class EndCallDirective:
    pass


@dataclass(frozen=True)
class TransferDirective:
    transfer_type: TransferType
    destination:   str
    reason:        str


@dataclass(frozen=True)
class UnknownDirective:
    """Escape hatch for a directive tag DirectiveParser has no typed class
    for yet — lets an experimental/rare kind pass through (kind + raw
    attrs) without requiring a new dataclass before it can even be seen."""
    kind:  str
    attrs: dict[str, str] = field(default_factory=dict)


Directive = Union[EndCallDirective, TransferDirective, UnknownDirective]


@dataclass(frozen=True)
class TransferRequest:
    """
    Built from a TransferDirective, or from an escalation-threshold trigger
    (see PipelineConversationHandler.record_guardrail_violation), plus the
    session's own identifiers.

    Consumed by servicer.py: published as a TransferRequested event
    (observability) and sent to the gateway as a TransferRequest gRPC
    message — held until the acknowledgment turn's audio finishes playing —
    which the gateway executes over ESL (uuid_transfer).
    """
    session_id:    str
    tenant_id:     str
    call_id:       str
    transfer_type: TransferType
    destination:   str
    reason:        str
    trigger:       str = "llm_directive"   # or "escalation_threshold"
    # Observability-only correlation id, one fresh UUID per attempt — rides
    # the TransferRequest gRPC message and is echoed back on TransferInitiated/
    # Completed/Failed so gateway and service logs join on it (Phase 5F).
    transfer_id:   str = field(default_factory=lambda: uuid.uuid4().hex)
    # What caller ID the human agent sees on a warm transfer's agent leg —
    # already resolved (agent.caller_id_policy/platform_did/custom_caller_id,
    # see transfer_engine.py's _resolve_caller_id()) by the time this
    # dataclass is built. Empty string (the default) means "use the
    # caller's own ANI" — the gateway's pre-existing behavior, and the only
    # meaningful value for cold transfer (no equivalent there).
    caller_id:     str = ""
    # What the caller experiences while a warm transfer's agent leg is
    # ringing — a raw passthrough of agent.transfer_waiting_experience, NOT
    # resolved here (unlike caller_id above): whether to call uuid_hold is
    # a telephony-execution decision the gateway's WarmTransferCoordinator
    # makes for itself. Empty string (the default) means "announcement_moh"
    # — the gateway's pre-existing behavior.
    waiting_experience: str = ""


@dataclass(frozen=True)
class DirectiveResult:
    """Returned by DirectiveParser.parse(): clean_text is the input with
    every complete directive tag stripped out (safe for a sentence
    splitter/TTS); directives is everything found, in the order
    encountered."""
    clean_text: str
    directives: list[Directive] = field(default_factory=list)


class StreamBuffer:
    """
    Buffers streamed text chunk-by-chunk. feed() returns only the portion
    safe to hand to a parser — an unterminated "[[...]]" tail is held back
    until a later feed() call supplies its closing "]]". Purely
    bracket-matching; has no notion of what a valid directive name or
    attribute looks like (that's DirectiveParser's job).
    """

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, chunk: str) -> str:
        """Feed one chunk of streamed text. Returns text now safe to
        parse — zero or more complete [[...]] tags plus any surrounding
        text — never a partial tag fragment."""
        self._pending += chunk
        last_open = self._pending.rfind("[[")
        if last_open != -1 and "]]" not in self._pending[last_open:]:
            safe, self._pending = self._pending[:last_open], self._pending[last_open:]
        else:
            safe, self._pending = self._pending, ""
        return safe

    def flush(self) -> str:
        """Call once at end of stream. Whatever is still pending never
        closed into a complete tag — a false-positive lookalike (the model
        literally said "[[" in prose, not a real directive) — so it's
        returned as ordinary text rather than silently dropped."""
        remainder, self._pending = self._pending, ""
        return remainder


class DirectiveParser:
    """Pure str -> DirectiveResult parsing. Only ever called with text
    containing complete tags (or none) — never a partial one; see
    StreamBuffer, which guarantees that."""

    @staticmethod
    def parse(text: str) -> DirectiveResult:
        directives: list[Directive] = []

        def _record(m: "re.Match[str]") -> str:
            directives.append(DirectiveParser._build(
                m.group("name"), dict(_ATTR_RE.findall(m.group("attrs"))),
            ))
            return ""

        clean_text = _TAG_RE.sub(_record, text)
        clean_text = strip_markdown_chars(clean_text)
        return DirectiveResult(clean_text=clean_text, directives=directives)

    @staticmethod
    def _build(name: str, attrs: dict[str, str]) -> Directive:
        if name == "END_CALL":
            return EndCallDirective()
        if name == "TRANSFER":
            return TransferDirective(
                transfer_type=coerce_transfer_type(attrs.get("type", "none")),
                destination=attrs.get("destination", ""),
                reason=attrs.get("reason", ""),
            )
        return UnknownDirective(kind=name, attrs=attrs)
