"""
Directive detection for streamed LLM output.

A "directive" is an inline control token the LLM emits in its response text
to signal something other than spoken words — e.g. [[END_CALL]] (no
attributes) or [[TRANSFER type="warm" destination="+1555..." reason="..."]]
(attributes). Three components, each with one job:

  StreamBuffer   — buffers streamed chunks, holds back an *unterminated*
                   "[[...]]" region so it's never handed out as "safe to
                   use" text. Knows nothing about directive names/attrs —
                   purely bracket-matching.
  DirectiveParser — pure str -> DirectiveResult parsing. Only ever called
                   with text StreamBuffer has confirmed contains complete
                   tags (or none). Knows nothing about streaming/chunking.
  DirectiveResult — clean_text (safe for the sentence splitter/TTS) +
                   directives (everything found, typed, in order).

Pipeline (see pipeline.py's _llm_to_tts):

    LLM token stream -> StreamBuffer.feed() -> safe text (no partial tags)
                     -> DirectiveParser.parse() -> DirectiveResult
    result.clean_text  -> sentence splitter -> TTS   (never sees a directive)
    result.directives  -> session/servicer            (never touches TTS)

Adding a new directive kind (e.g. a future [[ESCALATE]] or [[PLAY ...]])
needs a new typed dataclass and one branch in DirectiveParser._build() —
no new buffering/streaming logic anywhere.

Streaming-safety rationale: a directive's attribute values (e.g.
reason="the issue is resolved. thanks") can contain sentence-ending
punctuation that would otherwise confuse a downstream sentence splitter —
chopping a live tag in half and reading raw "[[TRANSFER ..." fragments
aloud. StreamBuffer.feed() prevents that by construction: text is never
returned as "safe" while a "[[" it contains hasn't yet been closed by "]]".
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Union

_TAG_RE  = re.compile(r'\[\[(?P<name>[A-Z_]+)(?P<attrs>[^\]]*)\]\]')
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

# LLMs occasionally emit markdown (**bold**, bullet lists, headers) even
# when never asked to — harmless as text, but a TTS engine speaks these
# characters literally (e.g. "*" -> "asterisk"); confirmed live 2026-08-21,
# spoken right before a time ("**Time:** 10:00 AM"). A blanket strip rather
# than a paired **...** regex on purpose: DirectiveParser.parse() is called
# per streamed chunk (see StreamBuffer.feed()), and a bold marker can land
# split across two chunks — stripping every occurrence of these characters
# unconditionally is naturally robust to that, no pairing/state needed.
_MARKDOWN_CHARS_RE = re.compile(r"[*_`#]")


class TransferType(str, Enum):
    """
    Mirrors database/schema.sql's agents.transfer_type CHECK constraint
    ('warm', 'cold', 'none'). Deliberately duplicated rather than shared:
    the DB constraint validates persistence, this enum gives the
    Conversation Service's domain logic (TransferDirective/TransferRequest)
    real type-checking instead of raw string comparisons scattered around.
    No shared source of truth generates one from the other today — if the
    DB's allowed values ever change, both need updating.

    Inherits str so equality against a raw config string still works
    (TransferType.WARM == "warm"), but str(TransferType.WARM) is
    "TransferType.WARM", not "warm" — use `.value` wherever the plain
    string is needed (logging, the TransferRequested event, escalation
    defaults sourced from RuntimeConfig.policies.transfer_type).
    """
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
        clean_text = _MARKDOWN_CHARS_RE.sub("", clean_text)
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
