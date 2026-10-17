"""LLMAdapter feature detection: generate_with_tools() when tools are offered and supported, else wrapped generate()."""

from __future__ import annotations

from services.conversation.providers.interfaces import ChatMessage
from services.conversation.tools.llm_adapter import LLMAdapter, TokenEvent, ToolCallEvent

_SCHEMAS = [{"name": "book_appointment", "description": "d", "parameters": {"type": "object"}}]


class _PlainLLM:
    """Implements only ILLM — no generate_with_tools() at all."""

    async def generate(self, messages):
        for t in ["Hi", " there"]:
            yield t


class _ToolAwareLLM:
    """Implements ILLM AND IToolAwareLLM."""

    def __init__(self, tool_call: bool) -> None:
        self._tool_call = tool_call

    async def generate(self, messages):
        yield "should not be called when tools offered"

    async def generate_with_tools(self, messages, schemas, tool_choice=None):
        self.seen_schemas = schemas
        self.seen_tool_choice = tool_choice
        if self._tool_call:
            yield ToolCallEvent(tool_call_id="c1", tool_name="book_appointment", arguments={"start_time": "x"})
        else:
            yield TokenEvent(text="plain answer")


async def test_falls_back_to_plain_generate_when_provider_not_tool_aware():
    adapter = LLMAdapter(_PlainLLM())
    events = [e async for e in adapter.generate([ChatMessage(role="user", content="hi")], _SCHEMAS)]

    assert events == [TokenEvent(text="Hi"), TokenEvent(text=" there")]


async def test_falls_back_to_plain_generate_when_no_schemas_offered():
    llm = _ToolAwareLLM(tool_call=False)
    adapter = LLMAdapter(llm)
    events = [e async for e in adapter.generate([ChatMessage(role="user", content="hi")], schemas=None)]

    # Even though llm supports tools, no schemas means plain generate() is used.
    assert events == [TokenEvent(text="should not be called when tools offered")]


async def test_uses_generate_with_tools_when_schemas_offered_and_supported():
    llm = _ToolAwareLLM(tool_call=True)
    adapter = LLMAdapter(llm)
    events = [e async for e in adapter.generate([ChatMessage(role="user", content="book 3pm")], _SCHEMAS)]

    assert len(events) == 1
    assert isinstance(events[0], ToolCallEvent)
    assert events[0].tool_name == "book_appointment"
    assert llm.seen_schemas == _SCHEMAS


async def test_tool_aware_provider_can_still_answer_plainly():
    llm = _ToolAwareLLM(tool_call=False)
    adapter = LLMAdapter(llm)
    events = [e async for e in adapter.generate([ChatMessage(role="user", content="say hi")], _SCHEMAS)]

    assert events == [TokenEvent(text="plain answer")]


_KB = [{"name": "search_knowledge", "description": "d", "parameters": {"type": "object"}}]
_MSGS = [ChatMessage(role="user", content="hi")]


class _TextLLM:
    """Streams the given chunks as reply text, on both the plain and the tool-aware path."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def generate(self, messages):
        for c in self._chunks:
            yield c

    async def generate_with_tools(self, messages, schemas, tool_choice=None):
        for c in self._chunks:
            yield TokenEvent(text=c)


def _chunked(text: str, size: int = 5) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


async def _run(chunks, schemas):
    return [e async for e in LLMAdapter(_TextLLM(chunks)).generate(_MSGS, schemas)]


def _spoken(events) -> str:
    return "".join(e.text for e in events if isinstance(e, TokenEvent))


async def test_text_form_call_to_offered_tool_becomes_tool_call():
    text = '{"name": "searchknowledge", "parameters": {"query": "fees"}}'
    events = await _run(_chunked(text), _KB)

    assert [(e.tool_name, e.arguments) for e in events] == [("search_knowledge", {"query": "fees"})]


async def test_function_wrapper_and_arguments_key_are_accepted():
    text = '{"type": "function", "function": {"name": "search_knowledge", "arguments": {"query": "x"}}}'
    events = await _run(_chunked(text), _KB)

    assert [(e.tool_name, e.arguments) for e in events] == [("search_knowledge", {"query": "x"})]


async def test_fenced_text_form_call_becomes_tool_call():
    text = '```json\n{"name": "search_knowledge", "parameters": {"query": "x"}}\n```'
    events = await _run(_chunked(text, 3), _KB)

    assert [(e.tool_name, e.arguments) for e in events] == [("search_knowledge", {"query": "x"})]


async def test_text_form_call_to_unknown_tool_is_never_spoken():
    events = await _run(_chunked('{"name": "delete_everything", "parameters": {}}'), _KB)

    assert events == []


async def test_text_form_call_without_tools_is_never_spoken():
    events = await _run(_chunked('{"name": "search_knowledge", "parameters": {"query": "x"}}'), None)

    assert events == []


async def test_prose_streams_token_by_token_without_buffering():
    chunks = ["Hel", "lo ", "there"]
    events = await _run(chunks, _KB)

    assert events == [TokenEvent(text=c) for c in chunks]


async def test_json_that_is_not_a_tool_call_is_released_as_text():
    text = '{"answer": 42}'
    events = await _run(_chunked(text), _KB)

    assert _spoken(events) == text


async def test_non_json_brace_prefix_is_released_at_once():
    events = await _run(["{", "oops ", "later"], _KB)

    assert _spoken(events) == "{oops later"
    assert len(events) == 2  # "{" alone is held until the next character shows what follows


async def test_overlong_json_that_is_not_a_tool_call_is_released():
    text = '{"text": "' + "a" * 3000
    events = await _run(_chunked(text, 500), _KB)

    assert _spoken(events) == text


async def test_overlong_tool_call_shaped_json_is_never_spoken():
    text = '{"name": "' + "a" * 3000
    events = await _run(_chunked(text, 500), _KB)

    assert _spoken(events) == ""


async def test_a_cut_off_text_tool_call_is_never_spoken():
    from services.conversation.tools.llm_adapter import recover_text_tool_calls

    async def events():
        for chunk in ('{"name": "search_knowledge", ', '"parameters": {"query": "par'):
            yield TokenEvent(text=chunk)

    out = [e async for e in recover_text_tool_calls(events(), {"search_knowledge"})]
    assert out == []


async def test_unparseable_call_is_dropped_and_the_prose_after_it_is_spoken():
    events = await _run(_chunked('{"name": "x", "parameters": {"a": True}} Sure, one moment.'), _KB)

    assert _spoken(events).strip() == "Sure, one moment."
    assert not any(isinstance(e, ToolCallEvent) for e in events)


async def test_cut_off_call_is_never_spoken():
    assert await _run(_chunked('{"name": "search_knowledge", "parameters": {"query": "fe'), _KB) == []


async def test_over_long_call_is_never_spoken():
    assert await _run(_chunked('{"name": "x", "parameters": {"q": "' + "a" * 3000, 400), _KB) == []


async def test_plain_fence_is_recovered_like_a_json_fence():
    text = '```\n{"name": "search_knowledge", "parameters": {"query": "x"}}\n```'
    events = await _run(_chunked(text, 3), _KB)

    assert [(e.tool_name, e.arguments) for e in events] == [("search_knowledge", {"query": "x"})]


async def test_alternative_name_keys_are_recovered_or_dropped_never_spoken():
    for key in ("function", "tool_name", "tool"):
        events = await _run(_chunked('{"%s": "search_knowledge", "arguments": {"query": "x"}}' % key), _KB)
        assert [e.tool_name for e in events] == ["search_knowledge"]
    assert await _run(_chunked('{"tool": "nope", "arguments": {}}'), _KB) == []
    assert await _run(_chunked('{"name": "search_knowledge"}'), _KB) == []


async def test_text_call_ids_are_unique():
    text = '{"name": "search_knowledge", "parameters": {"query": "x"}}'
    first, second = (await _run(_chunked(text), _KB))[0], (await _run(_chunked(text), _KB))[0]

    assert first.tool_call_id != second.tool_call_id
