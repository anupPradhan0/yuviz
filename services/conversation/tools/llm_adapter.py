"""
LLMAdapter — the seam that keeps ILLM.generate() completely untouched while
still supporting tool-calling (see Tool Execution Framework design, review
point 1, 2026-07-22).

ILLM's formal contract never changes: generate(messages) -> AsyncGenerator[str].
A concrete provider MAY additionally implement IToolAwareLLM — a narrow,
optional companion interface with its own generate_with_tools() method on
the same class, reusing whatever client/auth that class already
constructed for ILLM.generate(). LLMAdapter feature-detects this via
hasattr() and falls back to plain generate() (wrapped in TokenEvents) when
it's absent, so an ILLM implementation that never supports tool-calling
needs zero new code.

Each provider's generate_with_tools() parses its own wire format directly
and yields already-normalized TurnEvents — no separate per-vendor parser
class. This mirrors how OllamaLLM/GeminiLLM already parse their own token
streams inside generate() today; a parallel "parser" abstraction would be
indirection with no real use, since the parsing logic is only ever called
from inside that one provider's own method anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Protocol, runtime_checkable

from ..providers.interfaces import ChatMessage


class TurnEvent:
    """Marker base — see TokenEvent/ToolCallEvent."""


@dataclass(frozen=True)
class TokenEvent(TurnEvent):
    text: str


@dataclass(frozen=True)
class ToolCallEvent(TurnEvent):
    tool_call_id: str
    tool_name:    str
    arguments:    dict[str, Any] = field(default_factory=dict)
    # Opaque per-provider passthrough (e.g. Gemini's thoughtSignature, which
    # must be echoed back verbatim on this exact call when it's replayed
    # into history for a later turn — see gemini.py). Orchestrator/executor
    # code never reads this; only the same provider that set it does.
    provider_metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolCallStartedEvent(TurnEvent):
    """Yielded the instant a tool call is about to execute (before its
    round-trip, which can be a slow external API call) — lets the caller
    speak a short acknowledgment filler instead of leaving dead air for
    the whole tool duration. Confirmed as a real, unaddressed gap (Dograh's
    own equivalent is opt-in per-tool, manually configured; this is
    automatic, no per-tool setup needed)."""
    tool_name: str


@runtime_checkable
class IToolAwareLLM(Protocol):
    """Optional companion to ILLM. A provider class implements this
    ADDITIONALLY to ILLM, never instead of it — see module docstring."""

    def generate_with_tools(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]],
    ) -> AsyncGenerator[TurnEvent, None]:
        ...


class LLMAdapter:
    """Wraps one ILLM instance. Call generate() exactly like ILLM.generate()
    but with an extra `schemas` argument — always yields TurnEvent, never a
    bare string, regardless of whether the underlying provider actually
    supports tool-calling."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    async def generate(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        if schemas and isinstance(self._llm, IToolAwareLLM):
            async for event in self._llm.generate_with_tools(messages, schemas):
                yield event
            return

        # No tools offered this turn, or the provider doesn't support
        # tool-calling at all — plain generate(), wrapped uniformly so
        # ToolCallOrchestrator never has to know which case this is.
        async for token in self._llm.generate(messages):
            yield TokenEvent(text=token)
