import pytest

from services.conversation.providers.llm.fallback import FallbackLLM
from services.conversation.tools.llm_adapter import TokenEvent


class _FakeLLM:
    def __init__(self, tokens=None, tool_events=None, fail_before_yield=False, fail_after_yield=False):
        self._tokens = tokens or []
        self._tool_events = tool_events or []
        self._fail_before_yield = fail_before_yield
        self._fail_after_yield = fail_after_yield

    async def generate(self, messages):
        if self._fail_before_yield:
            raise RuntimeError("boom before any output")
        for t in self._tokens:
            yield t
            if self._fail_after_yield:
                raise RuntimeError("boom after some output")

    async def generate_with_tools(self, messages, schemas, tool_choice=None):
        if self._fail_before_yield:
            raise RuntimeError("boom before any output")
        for e in self._tool_events:
            yield e
            if self._fail_after_yield:
                raise RuntimeError("boom after some output")


@pytest.mark.asyncio
async def test_falls_back_to_secondary_when_primary_fails_before_any_output():
    primary = _FakeLLM(fail_before_yield=True)
    secondary = _FakeLLM(tokens=["hello", " world"])
    llm = FallbackLLM(primary, secondary, primary_name="p", secondary_name="s")

    tokens = [t async for t in llm.generate([])]

    assert tokens == ["hello", " world"]


@pytest.mark.asyncio
async def test_does_not_retry_when_primary_fails_after_yielding_output():
    primary = _FakeLLM(tokens=["partial"], fail_after_yield=True)
    secondary = _FakeLLM(tokens=["should never be used"])
    llm = FallbackLLM(primary, secondary, primary_name="p", secondary_name="s")

    with pytest.raises(RuntimeError, match="boom after some output"):
        _ = [t async for t in llm.generate([])]


@pytest.mark.asyncio
async def test_generate_with_tools_falls_back_before_any_output():
    primary = _FakeLLM(fail_before_yield=True)
    secondary = _FakeLLM(tool_events=[TokenEvent(text="ok")])
    llm = FallbackLLM(primary, secondary, primary_name="p", secondary_name="s")

    events = [e async for e in llm.generate_with_tools([], [])]

    assert events == [TokenEvent(text="ok")]


@pytest.mark.asyncio
async def test_primary_success_never_touches_secondary():
    primary = _FakeLLM(tokens=["fine"])
    secondary = _FakeLLM(fail_before_yield=True)  # would raise if ever called
    llm = FallbackLLM(primary, secondary, primary_name="p", secondary_name="s")

    tokens = [t async for t in llm.generate([])]

    assert tokens == ["fine"]
