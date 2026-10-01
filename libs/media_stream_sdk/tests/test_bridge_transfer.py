from __future__ import annotations

import asyncio
import json

import grpc
import grpc.aio
import pytest

from libs.media_stream_sdk.bridge import MediaStreamBridge
from libs.media_stream_sdk.serializers import VobizSerializer
from libs.vad_sdk.vad import VADEvent
from services.conversation.directives import TransferRequest, TransferType
from services.conversation.echo import EchoConversationHandler
from services.conversation.session import HandlerResponse
from services.conversation.servicer import ConversationServicer
from voiceai.v1 import conversation_pb2 as pb
from services.conversation.generated.voiceai.v1 import conversation_pb2_grpc as servicer_grpc


class _FakeCall:
    def __init__(self, messages):
        self._messages = messages

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield m


@pytest.mark.asyncio
async def test_transfer_request_is_answered_with_initiated_then_failed():
    bridge = MediaStreamBridge(
        serializer=VobizSerializer(), call_id="call-1",
        tenant_slug="t", agent_slug="a", direction="inbound",
    )
    request = pb.ServiceMessage(transfer_request=pb.TransferRequest(
        session_id="sess-1", transfer_type="cold", destination="+18005550100",
        reason="customer_requested", transfer_id="tx-1",
    ))

    await bridge._grpc_to_vobiz(None, _FakeCall([request]))

    initiated = bridge._grpc_write_queue.get_nowait()
    assert initiated.WhichOneof("payload") == "transfer_initiated"
    assert (initiated.transfer_initiated.transfer_type, initiated.transfer_initiated.transfer_id) == ("cold", "tx-1")
    sent = bridge._grpc_write_queue.get_nowait()
    assert sent.WhichOneof("payload") == "transfer_failed"
    assert sent.transfer_failed.session_id == "sess-1"
    assert sent.transfer_failed.destination == "+18005550100"
    assert sent.transfer_failed.reason == "unsupported_provider"
    assert sent.transfer_failed.transfer_id == "tx-1"
    assert bridge._grpc_write_queue.empty()


class _FakeProviderWs:
    """A provider media stream: one start event, then open until closed."""

    def __init__(self):
        self.sent: list[str] = []
        self.closed = asyncio.Event()

    async def iter_text(self):
        yield json.dumps({"event": "start", "start": {"streamId": "stream-1", "callId": "call-1"}})
        await self.closed.wait()

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


class _AnnouncingTransferHandler(EchoConversationHandler):
    """A transfer turn that speaks first, like a transfer_announcement."""

    async def on_speech_ended(self, session_id, audio, duration_ms, energy_db):
        yield HandlerResponse(
            stt_text="I want a human",
            stt_confidence=1.0,
            tts_payloads=[b"\x00\x01" * 32000],  # 2 s, long enough to barge into
            transfer_request=TransferRequest(
                session_id=session_id, tenant_id="t", call_id=session_id,
                transfer_type=TransferType.COLD, destination="+18005550100",
                reason="caller_requested_human",
            ),
        )


async def _until(cond, what: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


async def _run_bridge_against_servicer(monkeypatch, *, barge_in: bool):
    async def factory(ctx):
        return _AnnouncingTransferHandler()

    server = grpc.aio.server()
    servicer_grpc.add_ConversationServiceServicer_to_server(ConversationServicer(factory), server)
    port = server.add_insecure_port("[::]:0")
    await server.start()
    monkeypatch.setenv("CONVERSATION_SVC_TARGET", f"localhost:{port}")

    session: list[str] = []
    bridge = MediaStreamBridge(
        serializer=VobizSerializer(), call_id="call-1",
        tenant_slug="t", agent_slug="a", direction="inbound",
        on_session_start=session.append,
    )
    written: list[pb.GatewayMessage] = []
    queue_put = bridge._grpc_write_queue.put_nowait
    def record(msg):
        written.append(msg)
        queue_put(msg)
    monkeypatch.setattr(bridge._grpc_write_queue, "put_nowait", record)

    ws = _FakeProviderWs()
    run = asyncio.create_task(bridge.run(ws))
    try:
        await _until(lambda: session, "session start")
        bridge._start_turn()
        bridge._grpc_write_queue.put_nowait(pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(
            session_id=session[0], duration_ms=500,
        )))
        if barge_in:
            await _until(lambda: bridge._playing_tts, "announcement playback")
            bridge._vad.process = lambda frame: VADEvent.SPEECH_START
            await bridge._run_vad(ws, session[0], b"\x00" * 1024)
        kinds = lambda: [m.WhichOneof("payload") for m in written]
        await _until(lambda: "playback_finished" in kinds(), "playback_finished")
        await asyncio.sleep(0.5)
        return written, ws
    finally:
        ws.closed.set()
        try:
            await asyncio.wait_for(run, timeout=5)
        except asyncio.TimeoutError:
            run.cancel()
        await server.stop(grace=0)


@pytest.mark.asyncio
async def test_spoken_announcement_then_transfer_fails_fast_through_the_servicer(monkeypatch):
    written, ws = await _run_bridge_against_servicer(monkeypatch, barge_in=False)

    kinds = [m.WhichOneof("payload") for m in written]
    assert any('"playAudio"' in s for s in ws.sent), "announcement was never played"
    finished = [m.playback_finished for m in written if m.WhichOneof("payload") == "playback_finished"]
    assert [f.interrupted for f in finished] == [False]
    assert kinds.index("playback_finished") < kinds.index("transfer_initiated") < kinds.index("transfer_failed")
    failed = next(m.transfer_failed for m in written if m.WhichOneof("payload") == "transfer_failed")
    assert (failed.destination, failed.reason) == ("+18005550100", "unsupported_provider")


@pytest.mark.asyncio
async def test_barge_in_during_announcement_drops_the_held_transfer(monkeypatch):
    written, _ = await _run_bridge_against_servicer(monkeypatch, barge_in=True)

    kinds = [m.WhichOneof("payload") for m in written]
    finished = [m.playback_finished for m in written if m.WhichOneof("payload") == "playback_finished"]
    assert [f.interrupted for f in finished] == [True]
    assert "transfer_initiated" not in kinds and "transfer_failed" not in kinds
