from services.conversation.directives import TransferRequest, TransferType
from services.conversation.servicer import _consume_pending_transfer


def _make_request(trigger: str) -> TransferRequest:
    return TransferRequest(
        session_id="s1", tenant_id="t1", call_id="c1",
        transfer_type=TransferType.WARM, destination="1000",
        reason="some reason", trigger=trigger,
    )


def test_llm_directive_transfer_dropped_on_barge_in():
    """The caller's own request — if they still want a human, they'll
    just ask again, so a barge-in silently dropping it is fine."""
    tr = _make_request("llm_directive")
    assert _consume_pending_transfer(tr, interrupted=True, sid="s1") is None


def test_escalation_transfer_survives_barge_in():
    """Confirmed live: a caller saying "Thank you" right after
    a fabricated "booked!" claim silently cancelled the safety-net
    transfer meant to catch exactly that — a system-initiated escalation
    must not be droppable just because the caller said something in
    between."""
    tr = _make_request("escalation_threshold")
    out = _consume_pending_transfer(tr, interrupted=True, sid="s1")
    assert out is not None
    assert out.transfer_request.destination == "1000"


def test_llm_directive_transfer_sent_when_not_interrupted():
    tr = _make_request("llm_directive")
    out = _consume_pending_transfer(tr, interrupted=False, sid="s1")
    assert out is not None
    assert out.transfer_request.destination == "1000"


def test_transfer_with_empty_destination_is_refused_regardless_of_trigger():
    tr = _make_request("escalation_threshold")
    tr = TransferRequest(
        session_id="s1", tenant_id="t1", call_id="c1",
        transfer_type=TransferType.WARM, destination="",
        reason="some reason", trigger="escalation_threshold",
    )
    assert _consume_pending_transfer(tr, interrupted=False, sid="s1") is None
