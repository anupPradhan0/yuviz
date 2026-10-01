from __future__ import annotations

import pytest

from libs.media_stream_sdk.bridge import MediaStreamBridge
from libs.media_stream_sdk.serializers import VobizSerializer
from voiceai.v1 import conversation_pb2 as pb


class _FakeCall:
    def __init__(self, messages):
        self._messages = messages

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield m


@pytest.mark.asyncio
async def test_transfer_request_is_answered_with_immediate_transfer_failed():
    bridge = MediaStreamBridge(
        serializer=VobizSerializer(), call_id="call-1",
        tenant_slug="t", agent_slug="a", direction="inbound",
    )
    request = pb.ServiceMessage(transfer_request=pb.TransferRequest(
        session_id="sess-1", transfer_type="cold", destination="+18005550100",
        reason="customer_requested", transfer_id="tx-1",
    ))

    await bridge._grpc_to_vobiz(None, _FakeCall([request]))

    sent = bridge._grpc_write_queue.get_nowait()
    assert sent.WhichOneof("payload") == "transfer_failed"
    assert sent.transfer_failed.session_id == "sess-1"
    assert sent.transfer_failed.destination == "+18005550100"
    assert sent.transfer_failed.reason == "unsupported_provider"
    assert sent.transfer_failed.transfer_id == "tx-1"
    assert bridge._grpc_write_queue.empty()
