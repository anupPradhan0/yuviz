"""
Asyncio EventBus for the ConversationService.

Design rules:
- Events are frozen dataclasses (immutable after publish).
- Subscribers register for a specific event type (exact class, no inheritance).
- publish() is non-blocking: events are put on an asyncio.Queue.
- The bus drains its queue on its own asyncio task — subscribers are called
  in the order they subscribed, serially per event.
- RecordingEventBus is the test double: records all published events.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Type, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")
AsyncHandler = Callable[[Any], Coroutine[Any, Any, None]]


# ---------------------------------------------------------------------------
# Event base
# ---------------------------------------------------------------------------

class Event:
    """Marker base for all bus events. Subclasses should be frozen dataclasses."""


# ---------------------------------------------------------------------------
# Domain events (frozen dataclasses)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionStateChanged(Event):
    session_id:      str
    from_state:      str
    to_state:        str
    trigger:         str
    prev_duration_ms: float = 0.0


@dataclass(frozen=True)
class SpeechStarted(Event):
    session_id: str
    energy_db:  float = 0.0


@dataclass(frozen=True)
class SpeechEnded(Event):
    session_id:  str
    duration_ms: int   = 0
    energy_db:   float = 0.0


@dataclass(frozen=True)
class TranscriptReady(Event):
    session_id: str
    text:       str
    confidence: float = 1.0


@dataclass(frozen=True)
class ResponseReady(Event):
    session_id: str
    text:       str


@dataclass(frozen=True)
class TtsChunkReady(Event):
    session_id:  str
    sequence_num: int
    payload:     bytes
    is_final:    bool = False


@dataclass(frozen=True)
class SessionEnded(Event):
    session_id: str
    reason:     str = "caller_hangup"


@dataclass(frozen=True)
class TransferRequested(Event):
    """
    Published when pipeline.py detects a [[TRANSFER]] directive or an
    escalation-threshold breach (see directives.py's TransferRequest and
    PipelineConversationHandler.record_guardrail_violation). Observability
    only, same posture as every other event on this bus — the transfer
    itself is executed via the TransferRequest gRPC message servicer.py
    sends to the gateway, not by any subscriber here.
    """
    session_id:    str
    tenant_id:     str
    call_id:       str
    transfer_type: str   # TransferType.value — plain str at this boundary,
                          # same "enum internally, .value for observability/
                          # logging" convention as ConversationFSM's
                          # from_state/to_state on SessionStateChanged above
    destination:   str
    reason:        str
    trigger:       str = "llm_directive"
    transfer_id:   str = ""   # observability-only correlation id (Phase 5F)


# Phase 5B of AI-to-human transfer: the Gateway notifying the Conversation
# Service about what the telephony layer is actually doing with a transfer
# it's executing — the opposite direction from TransferRequested above
# (which is *this* service telling the world it wants a transfer). These
# three drive ConversationSession's own ConversationFSM.TRANSFERRING state
# (see fsm.py) and are otherwise observability-only: no LLM/prompt change,
# no fallback speech, no workflow change results from receiving them.

@dataclass(frozen=True)
class TransferInitiated(Event):
    """The gateway has issued uuid_transfer and is now waiting for
    FreeSWITCH to confirm the outcome. The AI session may be closed by the
    gateway at any point after this — this is the one guaranteed chance to
    react before that happens."""
    session_id:    str
    transfer_type: str
    destination:   str
    reason:        str
    transfer_id:   str = ""


@dataclass(frozen=True)
class TransferCompleted(Event):
    """Confirmed: the destination answered and bridged."""
    session_id:  str
    destination: str
    transfer_id: str = ""


@dataclass(frozen=True)
class TransferFailed(Event):
    """Confirmed or presumed failed: hung up before bridging, the command
    was never accepted, or CallFSM's own TransferTimeout elapsed with no
    confirming event ever arriving (reason="transfer_timeout" in that
    case)."""
    session_id:  str
    destination: str
    reason:      str
    transfer_id: str = ""


# Phase 5D of AI-to-human transfer: graceful session finalization after a
# successful transfer — see session_finalizer.py and fsm.py's FINALIZING
# state. SessionFinalizing brackets the start of cleanup;
# ConversationFinalized marks it done (and is also sent to the gateway as a
# gRPC message — see servicer.py — so it can safely tear its own side down).
#
# Named ConversationFinalized, not SessionFinalized: "session" means a
# telephony/media session in the gateway and a per-call AI/business state
# container here — this event only means "the Conversation Service's own
# cleanup is done," not "the gateway's resources are released" (that's a
# separate concern, entirely the gateway's own once it receives this).

@dataclass(frozen=True)
class SessionFinalizing(Event):
    session_id: str
    reason:     str = "transfer_completed"


@dataclass(frozen=True)
class ConversationFinalized(Event):
    session_id:         str
    reason:             str  = "TRANSFER_SUCCESS"
    summary:            str  = ""
    summary_generated:  bool = False
    transcript_written: bool = False


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class EventBus:
    """
    Asyncio event bus.  Subscribers are async callables keyed by event type.
    Use ``async with bus:`` (or call start/stop explicitly) to run the drain loop.
    """

    def __init__(self, *, queue_size: int = 256) -> None:
        self._queue:       asyncio.Queue[Event] = asyncio.Queue(maxsize=queue_size)
        self._subscribers: dict[type, list[AsyncHandler]] = {}
        self._task:        asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._drain_loop())

    async def drain(self) -> None:
        """Wait until all currently queued events have been delivered."""
        await self._queue.join()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def __aenter__(self) -> "EventBus":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ── Pub / sub ──────────────────────────────────────────────────────────────

    def subscribe(self, event_type: Type[T], handler: AsyncHandler) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    def publish(self, event: Event) -> None:
        """Non-blocking: drops event and logs if queue is full."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("EventBus queue full — dropping %s", type(event).__name__)

    async def publish_async(self, event: Event) -> None:
        """Awaitable publish — blocks until space is available."""
        await self._queue.put(event)

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _drain_loop(self) -> None:
        while True:
            event = await self._queue.get()
            handlers = self._subscribers.get(type(event), [])
            for handler in handlers:
                try:
                    await handler(event)
                except Exception:
                    log.exception("EventBus handler error for %s", type(event).__name__)
            self._queue.task_done()


# ---------------------------------------------------------------------------
# RecordingEventBus — test double
# ---------------------------------------------------------------------------

class RecordingEventBus(EventBus):
    """
    Drop-in replacement for EventBus in unit tests.

    publish() records the event synchronously *and* enqueues for the drain loop
    so subscribers still fire if the loop is running.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)
        super().publish(event)

    def published_of(self, event_type: Type[T]) -> list[T]:
        return [e for e in self.events if isinstance(e, event_type)]

    def clear(self) -> None:
        self.events.clear()
