"""
FallbackLLM — wraps a primary and a secondary ILLM/IToolAwareLLM instance,
retrying the whole turn against the secondary when the primary fails before
producing any output.

Why "before any output" is the line: generate()/generate_with_tools() are
streaming generators. If the primary already yielded partial output (tokens
spoken, or a tool call already emitted) and then fails, retrying on the
secondary would mean the caller hears a duplicated or inconsistent reply —
worse than the existing single apology-and-retry-next-turn behavior in
pipeline.py's _llm_to_tts(). So a mid-stream failure is re-raised as-is and
falls through to that existing handling instead of being swallowed here.

Built after a real Groq 429 (rate_limit_exceeded, TPM cap on the on-demand
tier) killed a live call with no recovery — see project memory. Ollama is
deliberately never used as the secondary in this pair: it was already
established this session as the weaker, less tool-call-reliable provider,
so using it as a fallback would just reintroduce the original
unreliable-tool-calling problem under the exact condition (load /
rate-limiting) most likely to trigger it.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

from ..interfaces import ChatMessage

log = logging.getLogger(__name__)


class FallbackLLM:
    def __init__(self, primary: Any, secondary: Any, *, primary_name: str, secondary_name: str) -> None:
        self._primary = primary
        self._secondary = secondary
        self._primary_name = primary_name
        self._secondary_name = secondary_name

    async def generate(self, messages: list[ChatMessage]) -> AsyncGenerator[str, None]:
        yielded_any = False
        try:
            async for token in self._primary.generate(messages):
                yielded_any = True
                yield token
            return
        except Exception:
            if yielded_any:
                raise
            log.warning(
                "FallbackLLM: primary=%s failed before yielding output — retrying on secondary=%s",
                self._primary_name, self._secondary_name, exc_info=True,
            )
        async for token in self._secondary.generate(messages):
            yield token

    async def generate_with_tools(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncGenerator[Any, None]:
        yielded_any = False
        try:
            async for event in self._primary.generate_with_tools(messages, schemas, tool_choice=tool_choice):
                yielded_any = True
                yield event
            return
        except Exception:
            if yielded_any:
                raise
            log.warning(
                "FallbackLLM: primary=%s failed before yielding output — retrying on secondary=%s",
                self._primary_name, self._secondary_name, exc_info=True,
            )
        async for event in self._secondary.generate_with_tools(messages, schemas, tool_choice=tool_choice):
            yield event

    async def aclose(self) -> None:
        for llm in (self._primary, self._secondary):
            aclose = getattr(llm, "aclose", None)
            if aclose is not None:
                await aclose()
