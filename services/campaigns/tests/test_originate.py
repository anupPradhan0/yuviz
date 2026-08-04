"""
originate.py tests — a fake local ESL server (plain asyncio TCP), no real
FreeSWITCH. These test that this module speaks the ASSUMED protocol
correctly (see originate.py's own module docstring on what's unverified);
they do not and cannot prove a real originate against a real trunk works.
"""

from __future__ import annotations

import asyncio

import pytest

from services.campaigns import originate


class _FakeEslServer:
    """Emulates just enough of ESL's inline-mode handshake + bgapi
    originate exchange to test our client against, with a configurable
    reply string for the originate command itself."""

    def __init__(self, originate_reply: str, auth_ok: bool = True) -> None:
        self.originate_reply = originate_reply
        self.auth_ok = auth_ok
        self.received_commands: list[str] = []
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"Content-Type: auth/request\n\n")
        await writer.drain()

        auth_line = await reader.readline()
        self.received_commands.append(auth_line.decode().strip())
        await reader.readline()  # blank line terminator
        if self.auth_ok:
            writer.write(b"Content-Type: command/reply\nReply-Text: +OK accepted\n\n")
        else:
            writer.write(b"Content-Type: command/reply\nReply-Text: -ERR invalid\n\n")
        await writer.drain()
        if not self.auth_ok:
            writer.close()
            return

        originate_line = await reader.readline()
        self.received_commands.append(originate_line.decode().strip())
        await reader.readline()
        writer.write(f"Content-Type: command/reply\nReply-Text: {self.originate_reply}\n\n".encode())
        await writer.drain()
        writer.close()


@pytest.fixture(autouse=True)
def _point_at_fake_server(monkeypatch):
    """Each test starts its own fake server and monkeypatches the module
    globals originate_call() reads its target from — done per-test inside
    the test body (needs the dynamically-assigned port), this fixture just
    ensures no test accidentally reaches a real ESL endpoint."""
    monkeypatch.setattr(originate, "_ESL_HOST", "127.0.0.1")


async def test_originate_call_success_returns_job_uuid(monkeypatch):
    server = _FakeEslServer("+OK Job-UUID: abc-123-def")
    port = await server.start()
    monkeypatch.setattr(originate, "_ESL_PORT", port)

    job_uuid = await originate.originate_call("+14155551234", "+14155550100")

    assert job_uuid == "abc-123-def"
    assert any("auth" in c for c in server.received_commands)
    assert any("bgapi originate" in c for c in server.received_commands)
    assert any("+14155551234" in c for c in server.received_commands)
    await server.stop()


async def test_originate_call_auth_rejected_raises(monkeypatch):
    server = _FakeEslServer("+OK", auth_ok=False)
    port = await server.start()
    monkeypatch.setattr(originate, "_ESL_PORT", port)

    with pytest.raises(originate.OriginateError, match="auth"):
        await originate.originate_call("+14155551234", "+14155550100")

    await server.stop()


async def test_originate_call_command_rejected_raises(monkeypatch):
    server = _FakeEslServer("-ERR DESTINATION_OUT_OF_ORDER")
    port = await server.start()
    monkeypatch.setattr(originate, "_ESL_PORT", port)

    with pytest.raises(originate.OriginateError, match="rejected"):
        await originate.originate_call("+14155551234", "+14155550100")

    await server.stop()


async def test_originate_call_no_server_listening_raises(monkeypatch):
    monkeypatch.setattr(originate, "_ESL_PORT", 1)  # nothing listens on port 1

    with pytest.raises(originate.OriginateError, match="cannot reach"):
        await originate.originate_call("+14155551234", "+14155550100")


def test_parse_job_event_success():
    # Real shape confirmed live against FreeSWITCH 2026-07-28 (see
    # originate.py's EslJobEventListener docstring): the outer envelope
    # (first arg here) never carries Job-UUID — it lives inside the body,
    # itself a header block + blank line + the bgapi command's reply text.
    body = "Event-Name: BACKGROUND_JOB\nJob-UUID: abc-123\nContent-Length: 20\n\n+OK channel-uuid-xyz"
    job_uuid, succeeded, detail = originate._parse_job_event(
        {"Content-Type": "text/event-plain", "Content-Length": "80"}, body,
    )
    assert job_uuid == "abc-123"
    assert succeeded is True


def test_parse_job_event_failure():
    body = "Event-Name: BACKGROUND_JOB\nJob-UUID: abc-123\nContent-Length: 15\n\n-ERR NO_ANSWER"
    job_uuid, succeeded, detail = originate._parse_job_event(
        {"Content-Type": "text/event-plain", "Content-Length": "70"}, body,
    )
    assert job_uuid == "abc-123"
    assert succeeded is False
    assert "NO_ANSWER" in detail
