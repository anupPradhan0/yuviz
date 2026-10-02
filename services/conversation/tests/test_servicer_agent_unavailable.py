"""AgentUnavailable from the handler factory ends the stream with one fatal
AGENT_UNAVAILABLE error — a refusal, never a fallback — and the test
credential carried on the open request is never logged."""

from __future__ import annotations

import logging

import grpc
import grpc.aio
import pytest

from ..servicer import ConversationServicer
from ..session import AgentUnavailable
from ..generated.voiceai.v1 import conversation_pb2 as pb
from ..generated.voiceai.v1 import conversation_pb2_grpc as pb_grpc

SENTINEL = "sentinel-credential-9f3c"


class _Handler:
    async def greeting(self, session_id):
        return []

    async def on_session_end(self, session_id, reason, final_state=None):
        pass

    def start_finalization(self, session_id):
        pass

    def record_live_stage(self, session_id, stage):
        pass


async def _serve(factory):
    server = grpc.aio.server()
    pb_grpc.add_ConversationServiceServicer_to_server(ConversationServicer(factory), server)
    port = server.add_insecure_port("[::]:0")
    await server.start()
    return f"localhost:{port}", server


def _open(**kw) -> pb.GatewayMessage:
    return pb.GatewayMessage(
        session_open=pb.SessionOpenRequest(
            protocol_version="1.0", session_id="s1", tenant_id="t1",
            codec=pb.AUDIO_CODEC_PCM_S16LE, sample_rate=16000, channels=1, **kw,
        )
    )


async def test_agent_unavailable_yields_one_fatal_error_and_ends_stream(caplog):
    seen = []

    async def factory(ctx):
        seen.append(ctx)
        raise AgentUnavailable()

    caplog.set_level(logging.DEBUG)
    addr, server = await _serve(factory)
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stream = pb_grpc.ConversationServiceStub(channel).Converse()
            await stream.write(_open(direction="test", test_credential=SENTINEL))
            msgs = [m async for m in stream]
    finally:
        await server.stop(grace=0)

    assert len(msgs) == 1
    err = msgs[0].error
    assert (err.code, err.message, err.fatal) == ("AGENT_UNAVAILABLE", "agent unavailable", True)
    assert seen[0].test_credential == SENTINEL
    assert SENTINEL not in caplog.text


async def test_session_without_credential_still_opens():
    async def factory(ctx):
        assert ctx.test_credential == ""
        return _Handler()

    addr, server = await _serve(factory)
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stream = pb_grpc.ConversationServiceStub(channel).Converse()
            await stream.write(_open())
            ready = await stream.read()
            assert ready.HasField("service_ready")
            await stream.done_writing()
    finally:
        await server.stop(grace=0)
