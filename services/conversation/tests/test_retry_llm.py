import pytest

from services.conversation.providers.llm.retry import RetryOnceLLM
from services.conversation.tools.llm_adapter import TokenEvent


class _FakeLLM:
    def __init__(self, tokens=None, tool_events=None, fail_first_n_calls=0, fail_after_yield=False):
        self._tokens = tokens or []
        self._tool_events = tool_events or []
        self._fail_first_n_calls = fail_first_n_calls
        self._fail_after_yield = fail_after_yield
        self.calls = 0

    async def generate(self, messages):
        self.calls += 1
        if self.calls <= self._fail_first_n_calls:
            raise RuntimeError("boom before any output")
        for t in self._tokens:
            yield t
            if self._fail_after_yield:
                raise RuntimeError("boom after some output")

    async def generate_with_tools(self, messages, schemas, tool_choice=None):
        self.calls += 1
        if self.calls <= self._fail_first_n_calls:
            raise RuntimeError("boom before any output")
        for e in self._tool_events:
            yield e
            if self._fail_after_yield:
                raise RuntimeError("boom after some output")


class _FakeLLMWithWarm(_FakeLLM):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.warmed = False

    async def warm(self):
        self.warmed = True


@pytest.mark.asyncio
async def test_warm_propagates_when_present():
    inner = _FakeLLMWithWarm()
    llm = RetryOnceLLM(inner, name="x")

    await llm.warm()

    assert inner.warmed is True


@pytest.mark.asyncio
async def test_warm_is_a_no_op_when_not_implemented():
    inner = _FakeLLM()  # no warm() method — e.g. a cloud provider
    llm = RetryOnceLLM(inner, name="x")

    await llm.warm()  # must not raise


@pytest.mark.asyncio
async def test_retries_once_when_first_call_fails_before_any_output():
    inner = _FakeLLM(fail_first_n_calls=1, tokens=["hello", " world"])
    llm = RetryOnceLLM(inner, name="x")

    tokens = [t async for t in llm.generate([])]

    assert tokens == ["hello", " world"]
    assert inner.calls == 2


@pytest.mark.asyncio
async def test_gives_up_when_both_calls_fail_before_any_output():
    inner = _FakeLLM(fail_first_n_calls=2)
    llm = RetryOnceLLM(inner, name="x")

    with pytest.raises(RuntimeError, match="boom before any output"):
        _ = [t async for t in llm.generate([])]


@pytest.mark.asyncio
async def test_does_not_retry_when_first_call_fails_after_yielding_output():
    inner = _FakeLLM(tokens=["partial"], fail_after_yield=True)
    llm = RetryOnceLLM(inner, name="x")

    with pytest.raises(RuntimeError, match="boom after some output"):
        _ = [t async for t in llm.generate([])]

    assert inner.calls == 1


@pytest.mark.asyncio
async def test_generate_with_tools_retries_once_before_any_output():
    inner = _FakeLLM(fail_first_n_calls=1, tool_events=[TokenEvent(text="ok")])
    llm = RetryOnceLLM(inner, name="x")

    events = [e async for e in llm.generate_with_tools([], [])]

    assert events == [TokenEvent(text="ok")]
    assert inner.calls == 2


@pytest.mark.asyncio
async def test_success_on_first_call_never_retries():
    inner = _FakeLLM(tokens=["fine"])
    llm = RetryOnceLLM(inner, name="x")

    tokens = [t async for t in llm.generate([])]

    assert tokens == ["fine"]
    assert inner.calls == 1
