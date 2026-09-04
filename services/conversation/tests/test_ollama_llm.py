"""
OllamaLLM tests use httpx.MockTransport — no real network call, no cost.
generate_with_tools() shapes mirror what was actually captured live against
a running Ollama server on 2026-07-22 (see project history), not a guessed
schema.
"""

from __future__ import annotations

import json

import httpx

from services.conversation.providers.interfaces import ChatMessage
from services.conversation.providers.llm.ollama import OllamaLLM
from services.conversation.tools.llm_adapter import ToolCallEvent, TokenEvent


def _ndjson_body(*lines: dict) -> bytes:
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode()


def _make_llm(handler) -> OllamaLLM:
    llm = OllamaLLM(model="llama3.2")
    llm._client = httpx.AsyncClient(base_url="http://localhost:11434", transport=httpx.MockTransport(handler))
    return llm


async def test_generate_yields_streamed_tokens():
    def handler(request: httpx.Request) -> httpx.Response:
        body = _ndjson_body(
            {"message": {"role": "assistant", "content": "Hello"}, "done": False},
            {"message": {"role": "assistant", "content": " world"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True},
        )
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    tokens = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert tokens == ["Hello", " world"]


async def test_warm_sends_a_real_request():
    seen = {"called": False}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["called"] = True
        body = _ndjson_body({"message": {"role": "assistant", "content": "hi"}, "done": True})
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    await llm.warm()

    assert seen["called"] is True


async def test_warm_swallows_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    llm = _make_llm(handler)
    await llm.warm()  # must not raise


async def test_generate_with_tools_sends_wrapped_schema():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        body = _ndjson_body({"message": {"role": "assistant", "content": "ok"}, "done": True})
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    schemas = [{"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}}]
    _ = [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="hi")], schemas)]

    assert seen_payload["tools"] == [{"type": "function", "function": schemas[0]}]


async def test_generate_with_tools_yields_tool_call_event():
    def handler(request: httpx.Request) -> httpx.Response:
        body = _ndjson_body(
            {
                "message": {
                    "role": "assistant", "content": "",
                    "tool_calls": [{
                        "id": "call_abc",
                        "function": {"name": "book_appointment", "arguments": {"start_time": "2026-07-23T15:00:00"}},
                    }],
                },
                "done": False,
            },
            {"message": {"role": "assistant", "content": ""}, "done": True},
        )
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    schemas = [{"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}}]
    events = [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="book 3pm tomorrow")], schemas)]

    assert len(events) == 1
    assert isinstance(events[0], ToolCallEvent)
    assert events[0].tool_call_id == "call_abc"
    assert events[0].tool_name == "book_appointment"
    assert events[0].arguments == {"start_time": "2026-07-23T15:00:00"}


async def test_generate_with_tools_still_streams_plain_text_incrementally():
    def handler(request: httpx.Request) -> httpx.Response:
        body = _ndjson_body(
            {"message": {"role": "assistant", "content": "Hi"}, "done": False},
            {"message": {"role": "assistant", "content": " there"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True},
        )
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    schemas = [{"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}}]
    events = [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="say hi")], schemas)]

    assert events == [TokenEvent(text="Hi"), TokenEvent(text=" there")]


async def test_generate_with_tools_narrows_schemas_to_forced_tool():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        body = _ndjson_body({"message": {"role": "assistant", "content": "ok"}, "done": True})
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    schemas = [
        {"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}},
        {"name": "cancel_appointment", "description": "Cancel it", "parameters": {"type": "object"}},
    ]
    forced = {"type": "function", "function": {"name": "book_appointment"}}
    _ = [e async for e in llm.generate_with_tools(
        [ChatMessage(role="user", content="hi")], schemas, tool_choice=forced,
    )]

    assert seen_payload["tools"] == [{"type": "function", "function": schemas[0]}]


async def test_generate_with_tools_sends_all_schemas_without_tool_choice():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        body = _ndjson_body({"message": {"role": "assistant", "content": "ok"}, "done": True})
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    schemas = [
        {"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}},
        {"name": "cancel_appointment", "description": "Cancel it", "parameters": {"type": "object"}},
    ]
    _ = [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="hi")], schemas)]

    assert seen_payload["tools"] == [{"type": "function", "function": s} for s in schemas]


async def test_generate_with_tools_shapes_tool_call_and_result_natively():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(200, content=_ndjson_body({"message": {"role": "assistant", "content": "Booked!"}, "done": True}))

    llm = _make_llm(handler)
    schemas = [{"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}}]
    messages = [
        ChatMessage(role="user", content="book 3pm tomorrow"),
        ChatMessage(role="assistant", content="", tool_calls=[
            {"id": "call_abc", "name": "book_appointment", "arguments": {"start_time": "2026-07-23T15:00:00"}},
        ]),
        ChatMessage(role="tool", content='{"status": "success", "booked": true}', tool_call_id="call_abc"),
    ]
    _ = [e async for e in llm.generate_with_tools(messages, schemas)]

    assistant_msg = next(m for m in seen_payload["messages"] if m["role"] == "assistant" and m.get("tool_calls"))
    assert assistant_msg["tool_calls"] == [{"id": "call_abc", "function": {"name": "book_appointment", "arguments": {"start_time": "2026-07-23T15:00:00"}}}]

    tool_msg = next(m for m in seen_payload["messages"] if m["role"] == "tool")
    assert tool_msg["content"] == '{"status": "success", "booked": true}'
