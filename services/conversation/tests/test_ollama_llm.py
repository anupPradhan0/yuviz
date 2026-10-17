"""OllamaLLM tests via httpx.MockTransport; tool-call shapes match a real Ollama server."""

from __future__ import annotations

import json

import httpx
import pytest

from services.conversation.providers.interfaces import ChatMessage
from services.conversation.providers.llm.ollama import OllamaLLM, OllamaProviderError
from services.conversation.tools.llm_adapter import ToolCallEvent, TokenEvent


def _ndjson_body(*lines: dict) -> bytes:
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode()


def _make_llm(handler, think: bool | None = None) -> OllamaLLM:
    llm = OllamaLLM(model="llama3.2", think=think)
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


async def test_generate_sends_no_think_key_by_default():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        body = _ndjson_body({"message": {"role": "assistant", "content": "ok"}, "done": True})
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert "think" not in seen_payload


async def test_generate_sends_think_true_when_enabled():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        body = _ndjson_body({"message": {"role": "assistant", "content": "ok"}, "done": True})
        return httpx.Response(200, content=body)

    llm = _make_llm(handler, think=True)
    _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert seen_payload["think"] is True


async def test_generate_sends_think_false_when_explicitly_disabled():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        body = _ndjson_body({"message": {"role": "assistant", "content": "ok"}, "done": True})
        return httpx.Response(200, content=body)

    llm = _make_llm(handler, think=False)
    _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert seen_payload["think"] is False


async def test_generate_with_tools_sends_no_think_key_by_default():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        body = _ndjson_body({"message": {"role": "assistant", "content": "ok"}, "done": True})
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    schemas = [{"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}}]
    _ = [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="hi")], schemas)]

    assert "think" not in seen_payload


async def test_generate_with_tools_sends_think_true_when_enabled():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        body = _ndjson_body({"message": {"role": "assistant", "content": "ok"}, "done": True})
        return httpx.Response(200, content=body)

    llm = _make_llm(handler, think=True)
    schemas = [{"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}}]
    _ = [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="hi")], schemas)]

    assert seen_payload["think"] is True


async def test_generate_with_tools_sends_think_false_when_explicitly_disabled():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        body = _ndjson_body({"message": {"role": "assistant", "content": "ok"}, "done": True})
        return httpx.Response(200, content=body)

    llm = _make_llm(handler, think=False)
    schemas = [{"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}}]
    _ = [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="hi")], schemas)]

    assert seen_payload["think"] is False


def test_construction_log_line_reflects_configured_think_value(caplog):
    with caplog.at_level("INFO"):
        OllamaLLM(model="llama3.2", think=True)
        OllamaLLM(model="llama3.2", think=None)

    think_true_lines = [r.getMessage() for r in caplog.records if "OllamaLLM model=" in r.message]
    assert len(think_true_lines) == 2
    assert think_true_lines[0] != think_true_lines[1]
    assert "think=True" in think_true_lines[0]
    assert "think=None" in think_true_lines[1]


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


async def test_400_thinking_rejection_retries_once_without_think_and_remembers_it():
    """A 400 on `think` retries once without it, and later turns skip `think` from the start."""
    payloads_seen = []
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads_seen.append(payload)
        call_count["n"] += 1
        if "think" in payload:
            return httpx.Response(400, content=b'{"error":"gemma3 does not support thinking"}')
        return httpx.Response(200, content=_ndjson_body(
            {"message": {"role": "assistant", "content": "ok"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True},
        ))

    llm = _make_llm(handler, think=False)

    tokens = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]
    assert tokens == ["ok"]
    assert len(payloads_seen) == 2  # the failing attempt, then the retry
    assert "think" in payloads_seen[0]
    assert "think" not in payloads_seen[1]

    # Second turn: no retry needed, straight to the working shape.
    tokens2 = [tok async for tok in llm.generate([ChatMessage(role="user", content="again")])]
    assert tokens2 == ["ok"]
    assert len(payloads_seen) == 3
    assert "think" not in payloads_seen[2]


async def test_non_thinking_related_400_is_not_swallowed():
    """A 400 unrelated to `think` still raises."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"error":"model not found"}')

    llm = _make_llm(handler, think=False)
    with pytest.raises(OllamaProviderError, match="^provider_error$"):  # a fixed code, never the response body
        _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]


async def test_404_warns_once_per_instance_naming_the_model_and_still_raises(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b'{"error":"model not found"}')

    llm = _make_llm(handler)
    with caplog.at_level("WARNING", logger="services.conversation.providers.llm.ollama"):
        for _ in range(2):
            with pytest.raises(OllamaProviderError, match="^provider_error$"):
                _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    warnings = [r for r in caplog.records if "not pulled" in r.getMessage()]
    assert len(warnings) == 1
    assert "llama3.2" in warnings[0].getMessage()


def _tags_handler(*names: str):
    return lambda request: httpx.Response(200, json={"models": [{"name": n} for n in names]})


@pytest.mark.asyncio
@pytest.mark.parametrize(("pulled", "expected"), [
    (("llama3.2:latest",), True),
    (("llama3.2",), True),
    (("qwen3:4b",), False),
    ((), False),
])
async def test_model_pulled_reads_the_tag_list(pulled, expected):
    assert await _make_llm(_tags_handler(*pulled)).model_pulled() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(200, text="<html>welcome</html>"),
    httpx.Response(200, json=["llama3.2"]),
    httpx.Response(200, json={"models": [{"model": "llama3.2"}]}),
], ids=["html", "top-level-list", "entry-without-name"])
async def test_model_pulled_treats_an_unrecognised_reply_as_pulled(response):
    assert await _make_llm(lambda request: response).model_pulled() is True


@pytest.mark.asyncio
async def test_model_pulled_treats_an_unreachable_ollama_as_pulled():
    def down(request):
        raise httpx.ConnectError("refused")

    assert await _make_llm(down).model_pulled() is True


@pytest.mark.asyncio
async def test_model_pulled_stops_reading_an_oversized_tag_list_and_logs_a_fixed_code(caplog):
    chunks_sent = []

    async def body():
        for _ in range(100):  # 100 MB offered; only ~1 MB may be read
            chunks_sent.append(1)
            yield b"x" * 1_000_000

    llm = _make_llm(lambda request: httpx.Response(200, content=body()))
    with caplog.at_level("WARNING"):
        assert await llm.model_pulled() is True
    assert len(chunks_sent) < 5
    assert "provider_response_too_large" in caplog.text


@pytest.mark.asyncio
async def test_model_pulled_asks_for_an_uncompressed_list_and_refuses_a_compressed_one(caplog):
    seen = {}

    def handler(request):
        seen["accept"] = request.headers["Accept-Encoding"]
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b"\x1f\x8b")

    with caplog.at_level("WARNING"):
        assert await _make_llm(handler).model_pulled() is True
    assert seen["accept"] == "identity"
    assert "provider_response_encoded" in caplog.text
