"""Issue #97, Conversation side: the Ollama LLM adapter takes a tenant's extra.base_url. Only its origin is used, the
address is guarded and pinned, and failures are fixed codes: nothing an internal service answers reaches the call,
the transcript or a log. Real sockets (the recording fake server from the Knowledge tests)."""

from __future__ import annotations

import json
import logging

import pytest

from libs import egress_guard as eg
from services.conversation.ai_provider_manager import ProviderConfig, _make_ollama
from services.conversation.providers.interfaces import ChatMessage
from services.conversation.providers.llm.ollama import OllamaLLM, OllamaProviderError
from services.knowledge.tests.test_egress_guard import MARKER, FakeServer


def _own_logs(caplog) -> str:
    """Our loggers only. httpx/httpcore log the status line at INFO/DEBUG for any endpoint a tenant may reach (public or
    allow-listed after the guard); that is the library's log, not an internal service's text."""
    return " ".join(r.getMessage() for r in caplog.records if r.name.startswith(("services.", "libs.")))


_MSG = [ChatMessage(role="user", content="hi")]
_OK = "\n".join(json.dumps(x) for x in (
    {"message": {"role": "assistant", "content": "Hello"}, "done": False},
    {"message": {"role": "assistant", "content": ""}, "done": True},
)) + "\n"


@pytest.fixture
def allow(monkeypatch):
    def _allow(*entries):
        monkeypatch.setenv(eg.ALLOWED_HOSTS_ENV, ",".join(entries))
    return _allow


async def _run(base_url: str, *, tools: bool = False, timeout_s: float = 5.0):
    llm = OllamaLLM(model="m", base_url=base_url, timeout_s=timeout_s)
    try:
        if tools:
            return [e async for e in llm.generate_with_tools(_MSG, [{"name": "t", "parameters": {}}])]
        return [t async for t in llm.generate(_MSG)]
    finally:
        await llm.aclose()


async def test_an_allow_listed_origin_streams_and_the_path_is_fixed(allow):
    async with FakeServer(lambda path: (200, _OK, {})) as srv:
        allow(f"127.0.0.1:{srv.port}")
        assert await _run(f"http://127.0.0.1:{srv.port}/") == ["Hello"]
        assert [e.text for e in await _run(f"http://127.0.0.1:{srv.port}", tools=True)] == ["Hello"]
        assert {p for p, _ in srv.requests} == {"/api/chat"}


@pytest.mark.parametrize("suffix", ["/api/pull#", "/api/pull", "?x=", "/api/pull?x=", "#"])
async def test_the_fragment_and_query_tricks_never_reach_another_path(allow, suffix):
    async with FakeServer(lambda path: (200, _OK, {})) as srv:
        allow(f"127.0.0.1:{srv.port}")
        with pytest.raises(OllamaProviderError) as e:
            await _run(f"http://127.0.0.1:{srv.port}{suffix}")
        assert str(e.value) == "provider_url_invalid" and e.value.permanent
        assert srv.connections == 0


@pytest.mark.parametrize("spelling", ["2130706433", "0x7f.0.0.1", "017700000001", "127.1", "[::1]", "[::ffff:127.0.0.1]", "0.0.0.0"])
async def test_unsafe_spellings_are_refused_before_any_connection(spelling):
    async with FakeServer() as srv:
        with pytest.raises(OllamaProviderError):
            await _run(f"http://{spelling}:{srv.port}")
        assert srv.connections == 0


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["93.184.216.34", "127.0.0.1"], ["169.254.169.254"], ["10.0.0.9", "93.184.216.34"]])
async def test_a_name_resolving_to_a_private_address_is_refused(monkeypatch, addresses):
    async def resolver(host, port):
        return addresses
    monkeypatch.setattr(eg, "_resolve", resolver)
    async with FakeServer() as srv:
        with pytest.raises(OllamaProviderError) as e:
            await _run(f"http://rebind.example.com:{srv.port}")
        assert str(e.value) == "provider_unreachable" and srv.connections == 0


async def test_dns_rebinding_cannot_swap_the_address(monkeypatch, allow):
    async with FakeServer(lambda path: (200, _OK, {})) as srv:
        allow(f"pinned.example:{srv.port}")
        calls = []

        async def resolver(host, port):
            calls.append(host)
            return ["127.0.0.1"] if len(calls) == 1 else ["10.9.9.9"]
        monkeypatch.setattr(eg, "_resolve", resolver)
        assert await _run(f"http://pinned.example:{srv.port}") == ["Hello"]
        assert len(calls) == 1 and srv.requests[0][1]["host"] == f"pinned.example:{srv.port}"


async def test_a_redirect_to_an_internal_host_is_not_followed(allow):
    async with FakeServer() as internal:
        async with FakeServer(lambda path: (307, "", {"location": f"http://127.0.0.1:{internal.port}/api/pull"})) as srv:
            allow(f"127.0.0.1:{srv.port}")
            with pytest.raises(OllamaProviderError) as e:
                await _run(f"http://127.0.0.1:{srv.port}")
            assert str(e.value) == "provider_error" and e.value.permanent
            assert internal.connections == 0


async def test_the_default_allow_list_is_the_operators_ollama(monkeypatch):
    monkeypatch.delenv(eg.ALLOWED_HOSTS_ENV, raising=False)
    async with FakeServer(lambda path: (200, _OK, {})) as srv:
        monkeypatch.setenv("OLLAMA_BASE_URL", f"http://localhost:{srv.port}")
        assert await _run(f"http://localhost:{srv.port}") == ["Hello"]
    monkeypatch.delenv("OLLAMA_BASE_URL")
    OllamaLLM(model="m")  # no base_url at all: localhost:11434 still builds with no configuration
    with pytest.raises(OllamaProviderError):
        OllamaLLM(model="m", base_url="http://127.0.0.1:6379")


@pytest.mark.parametrize(("status", "code", "permanent"), [
    (401, "provider_auth_failed", True), (403, "provider_auth_failed", True), (429, "provider_rate_limited", False),
    (500, "provider_error", False), (404, "provider_error", True), (503, "provider_error", False),
])
@pytest.mark.parametrize("tools", [False, True])
async def test_failures_are_fixed_codes_and_nothing_internal_leaks(allow, caplog, status, code, permanent, tools):
    body = json.dumps({"error": f"{MARKER} http://10.1.2.3:11434/api/pull"})
    caplog.set_level(logging.DEBUG)
    async with FakeServer(lambda path: (status, body, {})) as srv:
        allow(f"127.0.0.1:{srv.port}")
        with pytest.raises(OllamaProviderError) as e:
            await _run(f"http://127.0.0.1:{srv.port}", tools=tools)
    assert (str(e.value), e.value.permanent) == (code, permanent)
    seen = repr(e.value) + _own_logs(caplog) + repr(e.value.__cause__)
    assert MARKER not in seen and "10.1.2.3" not in seen and str(srv.port) not in repr(e.value)
    assert e.value.__suppress_context__


async def test_a_garbage_stream_line_is_not_logged(allow, caplog):
    caplog.set_level(logging.DEBUG)
    async with FakeServer(lambda path: (200, f"{MARKER} not json\n" + _OK, {})) as srv:
        allow(f"127.0.0.1:{srv.port}")
        assert await _run(f"http://127.0.0.1:{srv.port}") == ["Hello"]
    assert MARKER not in _own_logs(caplog)


async def test_refused_and_timed_out_look_the_same(allow):
    import socket
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed = probe.getsockname()[1]
    probe.close()
    async with FakeServer(hang=True) as hung:
        allow(f"127.0.0.1:{closed}", f"127.0.0.1:{hung.port}")
        outcomes = []
        for port in (closed, hung.port):
            with pytest.raises(OllamaProviderError) as e:
                await _run(f"http://127.0.0.1:{port}", timeout_s=0.5)
            outcomes.append((str(e.value), e.value.permanent))
    assert outcomes[0] == outcomes[1] == ("provider_unreachable", False)


async def test_the_factory_applies_the_guard_to_the_tenants_config():
    cfg = ProviderConfig(id="l1", role="llm", engine="ollama", extra={"base_url": "http://169.254.169.254/latest"})
    with pytest.raises(OllamaProviderError):
        await _make_ollama(cfg, None)
