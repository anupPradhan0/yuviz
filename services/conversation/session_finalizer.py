"""
SessionFinalizer — post-call cleanup after a successful transfer, run
before the gateway is told it may destroy its own CallSession (Phase 5D of
AI-to-human transfer — see fsm.py's FINALIZING state and session.py's
on_transfer_completed()).

Structured as a step pipeline (IFinalizationStep), the same extensibility
model this project already uses for providers (AI Provider Manager) and
telephony (IDidProvider) — adding a future cleanup action (CRM sync, S3
recording upload, a Kafka event, analytics, billing, audit) means writing
one new step and adding it to the list, never touching SessionFinalizer
itself.

Where a step names a component this project hasn't built (a ToolExecutor —
no tool orchestrator exists yet, Phase 6b per project memory; a distinct
MemoryManager or tracing system), that step is an honest, logged no-op —
not a stub call to an imaginary class. This matches the project's
established "honest empty, not a fake stub" precedent (see
libs.knowledge_sdk's get_tools()).

Idempotent per session_id: calling finalize() twice for the same session
runs the pipeline once and returns the cached result the second time —
necessary because the gateway may (rarely) end up calling this path more
than once for the same call (e.g. a retried notification).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from .metrics import IMetrics, NullMetrics
from .providers.interfaces import ChatMessage, ILLM
from .transcript_builder import TranscriptBuilder

log = logging.getLogger(__name__)

_SUMMARY_PROMPT = (
    "Summarize this conversation in 1-2 sentences for an internal call log. "
    "Focus on what the caller wanted and how it was resolved (or why it "
    "wasn't). Do not address the caller — this is an internal note, not a "
    "reply to them."
)


class FinalizationStatus(Enum):
    """Overall outcome of one finalize() run — distinct from individual
    step failures, which are always tolerated and logged (see
    SessionFinalizer._run_step). TIMED_OUT specifically means the summary
    step exceeded its own budget and a fallback was used; cleanup itself
    still completed."""
    RUNNING   = "running"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    FAILED    = "failed"


@dataclass
class FinalizationContext:
    """Mutable state threaded through the step pipeline. Each step reads
    what it needs and writes what it produces — e.g. SummaryStep writes
    `summary`/`summary_generated`, PersistSummaryStep reads `summary` and
    writes `transcript_written`."""
    session_id:   str
    history:      list[ChatMessage]
    llm:          ILLM | None
    cancel_event: object  # asyncio.Event | None — kept untyped to avoid a
                          # hard asyncio.Event import requirement for callers
                          # that pass None in tests
    transcripts:  TranscriptBuilder | None
    metrics:      IMetrics
    started_at:   float = field(default_factory=time.monotonic)
    # Set by SessionFinalizer.finalize() when start_summary_early() was
    # called for this session_id — SummaryStep awaits this instead of
    # starting a fresh generation (see SessionFinalizer's own docstring on
    # why: this is what actually saves the latency, since the task has
    # normally already completed by the time finalize() runs).
    precomputed_summary_task: "asyncio.Task[str] | None" = None

    summary:            str  = ""
    summary_generated:   bool = False
    transcript_written:  bool = False
    timed_out:           bool = False


@dataclass(frozen=True)
class FinalizationResult:
    """Immutable, public-facing outcome — what callers outside this module
    (pipeline.py, session.py) actually see. Deliberately narrower than
    FinalizationContext (which is this module's own working state)."""
    summary:            str
    summary_generated:  bool
    transcript_written:  bool
    status:             FinalizationStatus


class IFinalizationStep(Protocol):
    """One cleanup action. `name` is used for logging/error attribution
    only (see SessionFinalizer._run_step) — implementations should be
    stateless or session-agnostic; all per-session state lives on the
    FinalizationContext passed to run()."""
    name: str

    async def run(self, ctx: FinalizationContext) -> None: ...


# ── Steps ─────────────────────────────────────────────────────────────────────

class StopRuntimeStep:
    """Stops AgentRuntime — signals any in-flight LLM generation for this
    session to stop before SummaryStep starts its own call."""
    name = "stop_agent_runtime"

    async def run(self, ctx: FinalizationContext) -> None:
        if ctx.cancel_event is not None:
            ctx.cancel_event.set()


class StopToolExecutorStep:
    """No tool orchestrator exists yet (Phase 6b, not built — see project
    memory's AI-to-human transfer review). Honest no-op."""
    name = "stop_tool_executor"

    async def run(self, ctx: FinalizationContext) -> None:
        return None


class CancelTimersStep:
    """This pipeline owns no timers of its own; the only per-session
    timer-like state (the cancel event) is handled by StopRuntimeStep.
    Kept as its own named step for parity with the requirement list and
    its own log line if it ever needs one."""
    name = "cancel_timers"

    async def run(self, ctx: FinalizationContext) -> None:
        return None


class MemoryFlushStep:
    """No separate long-term memory store exists; "flushing" here means
    the in-memory history is finalized and ready to summarize (the next
    step) — nothing to await."""
    name = "flush_memory_manager"

    async def run(self, ctx: FinalizationContext) -> None:
        return None


class SummaryStep:
    """
    Generates the conversation summary via a real LLM call — bounded by
    timeout_s so a slow/hung LLM can never hold up finalization (and,
    transitively, the gateway's own teardown, which is waiting on
    ConversationFinalized). On timeout, uses a fixed fallback string and
    increments conversation_finalize_timeout_total; cleanup still
    completes promptly either way — LLM latency never holds the media
    lifecycle hostage.
    """
    name = "generate_summary"

    _FALLBACK = "Conversation summary unavailable (generation timed out)."

    def __init__(self, timeout_s: float = 3.0) -> None:
        self._timeout_s = timeout_s

    async def run(self, ctx: FinalizationContext) -> None:
        if ctx.precomputed_summary_task is not None:
            # Speculative generation kicked off by start_summary_early() —
            # see SessionFinalizer's docstring. Normally already done by
            # now (TransferInitiated -> ring/answer/bridge takes far longer
            # than a summary LLM call), so this await is near-instant; if
            # it isn't, the same timeout budget as the cold-start path
            # still applies rather than blocking finalize() indefinitely.
            try:
                ctx.summary = await asyncio.wait_for(
                    asyncio.shield(ctx.precomputed_summary_task), timeout=self._timeout_s,
                )
                ctx.summary_generated = bool(ctx.summary)
            except asyncio.TimeoutError:
                log.warning(
                    "SessionFinalizer: precomputed summary not ready after %.1fs session=%s",
                    self._timeout_s, ctx.session_id,
                )
                ctx.metrics.increment("conversation_finalize_timeout_total")
                ctx.summary = self._FALLBACK
                ctx.summary_generated = False
                ctx.timed_out = True
            except Exception:
                log.exception(
                    "SessionFinalizer: precomputed summary generation failed session=%s",
                    ctx.session_id,
                )
                ctx.summary = self._FALLBACK
                ctx.summary_generated = False
            return

        if not ctx.history or ctx.llm is None:
            return
        try:
            ctx.summary = await asyncio.wait_for(
                self._generate(ctx.history, ctx.llm), timeout=self._timeout_s,
            )
            ctx.summary_generated = bool(ctx.summary)
        except asyncio.TimeoutError:
            log.warning(
                "SessionFinalizer: summary generation timed out after %.1fs session=%s",
                self._timeout_s, ctx.session_id,
            )
            ctx.metrics.increment("conversation_finalize_timeout_total")
            ctx.summary = self._FALLBACK
            ctx.summary_generated = False
            ctx.timed_out = True

    @staticmethod
    async def _generate(history: list[ChatMessage], llm: ILLM) -> str:
        messages = list(history) + [ChatMessage(role="user", content=_SUMMARY_PROMPT)]
        chunks: list[str] = []
        async for token in llm.generate(messages):
            chunks.append(token)
        return "".join(chunks).strip()


class PersistSummaryStep:
    """Persists the summary (real or fallback) via the existing
    transcript_entries table — no separate long-term-memory schema exists
    to migrate to for this."""
    name = "persist_session_summary"

    async def run(self, ctx: FinalizationContext) -> None:
        if ctx.transcripts is None or not ctx.summary:
            return
        ctx.transcripts.record_turn(ctx.session_id, "[session_summary]", 1.0, ctx.summary, False)
        ctx.transcript_written = True


class MetricsStep:
    """Emits the final, always-run metrics for this finalize() call.
    conversation_finalize_timeout_total (see SummaryStep) is emitted
    separately, at the point of the timeout itself, not here."""
    name = "emit_metrics"

    async def run(self, ctx: FinalizationContext) -> None:
        ctx.metrics.increment("conversation_finalize_success_total")
        ctx.metrics.observe(
            "conversation_finalize_latency_ms", (time.monotonic() - ctx.started_at) * 1000.0,
        )


class TracingStep:
    """No tracing infrastructure exists in this codebase yet. Honest no-op."""
    name = "finish_tracing"

    async def run(self, ctx: FinalizationContext) -> None:
        return None


class ProviderCleanupStep:
    """ISTT/ILLM/ITTS (see providers/interfaces.py) have no
    close()/shutdown method: they are stateless, shared, per-process
    instances (ProviderBundle), not per-session resources. Honest no-op."""
    name = "close_provider_sessions"

    async def run(self, ctx: FinalizationContext) -> None:
        return None


def _default_steps() -> list[IFinalizationStep]:
    return [
        StopRuntimeStep(),
        StopToolExecutorStep(),
        CancelTimersStep(),
        MemoryFlushStep(),
        SummaryStep(),
        PersistSummaryStep(),
        MetricsStep(),
        TracingStep(),
        ProviderCleanupStep(),
    ]


# ── Orchestrator ──────────────────────────────────────────────────────────────

class SessionFinalizer:
    """One instance shared across sessions — stateless except for the
    idempotency/status caches below; every finalize() call carries
    whatever session-specific state it needs (history, the LLM instance,
    the session's cancel event) rather than this class keeping its own
    per-session dicts of that state, since PipelineConversationHandler
    already owns it.

    start_summary_early()/discard_pending_summary() are the one exception:
    they DO keep a small per-session dict (_pending_summary_tasks), because
    that task has to survive between the TransferInitiated call (which
    starts it) and either the eventual TransferCompleted call to finalize()
    (which consumes it) or a TransferFailed/TransferCancelled call to
    discard_pending_summary() (which throws it away) — see
    docs/warm_transfer_architecture.md §7. Only the summary generation is
    started early, not the whole finalize() pipeline: running
    StopRuntimeStep/PersistSummaryStep/MetricsStep before the call has
    actually ended would tear down in-flight state and poison the
    idempotency cache below for a transfer that hasn't succeeded yet.
    """

    def __init__(
        self,
        transcripts: TranscriptBuilder | None = None,
        metrics:     IMetrics | None = None,
        steps:       list[IFinalizationStep] | None = None,
    ) -> None:
        self._transcripts = transcripts
        self._metrics     = metrics if metrics is not None else NullMetrics()
        self._steps       = steps if steps is not None else _default_steps()
        self._results:  dict[str, FinalizationResult]   = {}
        self._statuses: dict[str, FinalizationStatus]   = {}
        self._pending_summary_tasks: dict[str, asyncio.Task] = {}

    def start_summary_early(
        self, session_id: str, history: list[ChatMessage], llm: ILLM | None,
    ) -> None:
        """Called on TransferInitiated (see session.py/pipeline.py) — kicks
        off the summary LLM call speculatively, in parallel with the
        gateway's ring/answer/bridge sequence, instead of waiting for
        finalize() to be called on TransferCompleted. The task always
        resolves to a string (never raises) so it's safe to leave
        un-awaited if the transfer ends up failing/cancelling — see
        discard_pending_summary().

        A pre-existing task for this session_id (e.g. a second
        TransferInitiated after a failed-then-retried transfer) is left in
        place rather than replaced: discard_pending_summary() is
        responsible for clearing it first on that path (see pipeline.py's
        on_transfer_failed/on_transfer_cancelled).
        """
        if session_id in self._pending_summary_tasks or not history or llm is None:
            return

        async def _generate_safe() -> str:
            try:
                return await SummaryStep._generate(history, llm)
            except Exception:
                log.exception(
                    "SessionFinalizer: speculative summary generation failed session=%s",
                    session_id,
                )
                return ""

        self._pending_summary_tasks[session_id] = asyncio.ensure_future(_generate_safe())

    def discard_pending_summary(self, session_id: str) -> None:
        """Called when a speculatively-started summary will never be used —
        the transfer it was started for failed or was cancelled before
        dispatch (see pipeline.py's on_transfer_failed/on_transfer_cancelled).
        Cancels the task if it's still running rather than letting it
        finish unattended."""
        task = self._pending_summary_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def finalize(
        self,
        session_id:   str,
        history:      list[ChatMessage],
        llm:          ILLM | None,
        cancel_event,
        reason:       str = "transfer_completed",
    ) -> FinalizationResult:
        if session_id in self._results:
            log.info("SessionFinalizer: already finalized session=%s — skipping", session_id)
            return self._results[session_id]

        self._statuses[session_id] = FinalizationStatus.RUNNING
        ctx = FinalizationContext(
            session_id=session_id, history=history, llm=llm,
            cancel_event=cancel_event, transcripts=self._transcripts, metrics=self._metrics,
            precomputed_summary_task=self._pending_summary_tasks.pop(session_id, None),
        )

        for step in self._steps:
            await self._run_step(step, ctx)

        status = FinalizationStatus.TIMED_OUT if ctx.timed_out else FinalizationStatus.COMPLETED
        result = FinalizationResult(
            summary=ctx.summary, summary_generated=ctx.summary_generated,
            transcript_written=ctx.transcript_written, status=status,
        )
        self._statuses[session_id] = status
        self._results[session_id] = result
        return result

    def status(self, session_id: str) -> FinalizationStatus | None:
        """Returns None if finalize() was never called (or has been
        forget()-ten) for this session_id."""
        return self._statuses.get(session_id)

    def forget(self, session_id: str) -> None:
        """Drops the idempotency cache entry for session_id — call once the
        gateway has actually torn the call down, so a (very unlikely)
        future session_id reuse doesn't return a stale cached result."""
        self._results.pop(session_id, None)
        self._statuses.pop(session_id, None)

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _run_step(self, step: IFinalizationStep, ctx: FinalizationContext) -> None:
        try:
            await step.run(ctx)
        except Exception:
            log.exception("SessionFinalizer: step '%s' failed session=%s", step.name, ctx.session_id)
