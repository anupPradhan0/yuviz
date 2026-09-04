"""
RetryOnceLLM — retries a single ILLM once on failure, before any output has
been yielded. Replaces the earlier cross-engine fallback design (a second,
weaker engine as a safety net): that added its own risk — a different
model's tool-calling quality, a translation layer for tool_choice — for a
benefit that mostly only mattered on rate-limited/free-tier accounts. A
same-engine retry recovers from the same transient failures without ever
handing the conversation to a different model.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Callable

from ..interfaces import ChatMessage

log = logging.getLogger(__name__)


class RetryOnceLLM:
    def __init__(self, llm: Any, *, name: str) -> None:
        self._llm = llm
        self._name = name

    async def _with_retry(self, make_call: Callable[[], AsyncGenerator[Any, None]]) -> AsyncGenerator[Any, None]:
        yielded_any = False
        try:
            async for item in make_call():
                yielded_any = True
                yield item
            return
        except Exception:
            if yielded_any:
                raise
            log.warning("RetryOnceLLM: %s failed before yielding output — retrying once", self._name, exc_info=True)
        async for item in make_call():
            yield item

    async def generate(self, messages: list[ChatMessage]) -> AsyncGenerator[str, None]:
        async for token in self._with_retry(lambda: self._llm.generate(messages)):
            yield token

    async def generate_with_tools(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncGenerator[Any, None]:
        async for event in self._with_retry(
            lambda: self._llm.generate_with_tools(messages, schemas, tool_choice=tool_choice)
        ):
            yield event

    async def warm(self) -> None:
        warm = getattr(self._llm, "warm", None)
        if warm is not None:
            await warm()

    async def aclose(self) -> None:
        aclose = getattr(self._llm, "aclose", None)
        if aclose is not None:
            await aclose()
