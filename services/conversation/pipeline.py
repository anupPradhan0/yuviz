"""
PipelineConversationHandler — IConversationHandler backed by real providers.

Flow per utterance (triggered by on_speech_ended):
  1. FasterWhisperSTT.transcribe(audio_buffer)     → transcript
  2. OllamaLLM.generate(history + transcript)       → token stream
  3. Buffer tokens into sentences; KokoroTTS.synthesize(sentence) → PCM per sentence
  4. Yield HandlerResponse(stt_text) once, then HandlerResponse(tts_payloads) per sentence.

Cancellation:
  on_cancel() sets an asyncio.Event that on_speech_ended() checks between pipeline
  stages.  If set, the generator stops early and yields nothing further.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import AsyncGenerator

from libs.config_sdk import RuntimeConfig, validate_transfer_timeout_ms
from libs.knowledge_sdk import IKnowledgeProvider, RetrievalPolicy

from .directives import (
    Directive,
    DirectiveParser,
    EndCallDirective,
    StreamBuffer,
    TransferDirective,
    TransferRequest,
)
from .guardrails import GuardrailCounter, GuardrailDetector
from .metrics import IMetrics, NullMetrics
from .provider_bundle import ProviderBundle
from .providers.interfaces import ChatMessage, SttResult
from .session import HandlerResponse
from .session_finalizer import FinalizationResult, SessionFinalizer
from .tools.llm_adapter import TokenEvent as ToolTokenEvent
from .tools.llm_adapter import ToolCallStartedEvent
from .tools.orchestrator import ToolCallOrchestrator
from .transcript_builder import TranscriptBuilder, TurnLatency
from .transfer_engine import (
    DecisionContext,
    TransferDecisionEngine,
    TransferTrigger,
    TriggerType,
)

log = logging.getLogger(__name__)

# Sentence boundary splitter.  Rules:
#   • Always split after ! or ? (never abbreviations).
#   • Split after . only when NOT preceded by a known title abbreviation
#     (Mr/Ms/Dr/Sr/Jr/St/Mt/vs) or a single uppercase letter (middle initial).
#     Uses fixed-length lookbehinds, compatible with Python's re module.
#   • Also split at end-of-string so the last sentence is always synthesised.
_SENTENCE_RE = re.compile(
    r'(?<=[!?])\s+'
    r'|(?<!Mr\.)(?<!Ms\.)(?<!Dr\.)(?<!Sr\.)(?<!Jr\.)(?<!St\.)(?<!Mt\.)(?<!vs\.)'
    r'(?<![A-Z]\.)(?<=[.])\s+'
    r'|(?<=[.!?])$'
)

# Sentinel the LLM appends to its final reply when it decides the call is
# over.  Detected/stripped by StreamBuffer+DirectiveParser (see
# directives.py) before TTS synthesis — the caller never hears it — and
# used to signal EndCall to the gateway once this turn's audio has fully
# streamed (see servicer.py). A marker token, rather than keyword-sniffing
# the response for "goodbye"/"bye", avoids false positives from casual
# mentions and needs no function-calling support from the LLM.
_END_CALL_MARKER = "[[END_CALL]]"
# The *condition* is per-agent configurable (agents.end_call_prompt — a
# "When ..." clause); the token mechanics below it are fixed and always
# appended by _build_end_call_instruction(), so a custom prompt can never
# break directive parsing. Empty/None config = this default, byte-identical
# to the historical hardcoded instruction.
_END_CALL_CONDITION_DEFAULT = (
    "When the conversation is genuinely finished (the caller says "
    "goodbye, has no more questions, or the issue is resolved)"
)


def _build_current_date_context() -> str:
    """Nothing anywhere told the LLM what 'today' actually is — confirmed
    live 2026-07-27: a caller asked to book 'tomorrow' and qwen2.5:7b
    resolved it to a date 3 days in the past, because it had no grounding
    for the current date at all and had to guess. Computed fresh per call
    (not baked into agent config) so it's always accurate regardless of how
    long the process has been running. UTC, matching CalendarExecutor's own
    _DEFAULT_TIMEZONE and every Cal.com call already made with
    timeZone=UTC — the same date convention already used throughout the
    booking flow, not a new one introduced here."""
    now = datetime.now(timezone.utc)
    return (
        f"\n\nToday's date is {now.strftime('%Y-%m-%d')} ({now.strftime('%A')}), UTC. "
        "Use this to resolve any relative date the caller mentions (e.g. \"tomorrow\", "
        "\"next Monday\", \"in two weeks\") into an exact date yourself before calling any tool."
    )


def _build_end_call_instruction(condition: str | None, scripted: bool = False) -> str:
    """scripted=True when the agent has a configured farewell_message: the
    LLM is told to emit only the token — pipeline.py speaks the scripted
    line itself (deterministic wording, no LLM paraphrase)."""
    cond = (condition or "").strip().rstrip(".,;") or _END_CALL_CONDITION_DEFAULT
    if scripted:
        return (
            f"\n\n{cond}, reply with ONLY the exact token {_END_CALL_MARKER} "
            "and no other words — the system will speak the farewell message "
            "itself. Never say the token out loud or explain it to the caller."
        )
    return (
        f"\n\n{cond}, end your "
        f"final reply with the exact token {_END_CALL_MARKER} on its own, after "
        "your spoken words. Only use this token when you are truly ending the "
        "call — never say it out loud or explain it to the caller."
    )

# Fallback farewell synthesised when the LLM emits the end-call marker with
# no spoken text (e.g. replying to "tear down the call" with only the
# marker). Without this, tts_started_sent never flips true on the gateway
# side and the servicer silently drops EndCall (see servicer.py) — the call
# would never hang up on its own, forcing the caller to disconnect manually.
_FALLBACK_GOODBYE = "Goodbye."

# Spoken when the LLM/tool-orchestrator stream raises (provider 5xx/429,
# network error, a bridging bug) partway through a turn. Without this the
# caller hears dead air for that whole turn — the exception was already
# swallowed here (see except block below) so the call itself survives, but
# silence reads as a dropped call to a real caller. Not scripted per-agent
# (like farewell_message) since this is a transport-failure fallback, not a
# conversational choice — same posture as _FALLBACK_GOODBYE above.
_FALLBACK_LLM_ERROR = "Sorry, I'm having a little trouble right now. Could you say that again?"

# Spoken the instant a tool call starts (see ToolCallStartedEvent) — a real,
# confirmed UX gap otherwise: some tool round-trips (a calendar API, for
# instance) take long enough that the caller sits in total silence with no
# indication anything is happening. Dograh's own equivalent is opt-in,
# configured per-tool by whoever authors it; this fires automatically for
# any tool, no per-agent/per-tool setup needed. Fixed, not per-agent
# configurable for now — same posture as _FALLBACK_GOODBYE/_FALLBACK_LLM_ERROR,
# revisit only if a real need for per-agent wording shows up.
_TOOL_CALL_FILLER = "Let me check that for you."

# Phase 3 of AI-to-human transfer (see project memory): [[TRANSFER ...]] is
# now detected the same streaming-safe way [[END_CALL]] always has been —
# via StreamBuffer+DirectiveParser, buffered and stripped mid-stream so a
# directive tag never reaches TTS (supersedes Phase 2's simpler post-hoc
# regex, which only ran on the fully-assembled turn text and had no
# defense against a live agent speaking the raw tag aloud).
#
# Fully wired end-to-end (live-verified 2026-07-16): the instruction below
# is auto-appended to the system prompt whenever the agent's policies
# configure a transfer (see __init__) — operators only set transfer_type/
# transfer_destination (Escalation tab in the admin UI), never prompt text,
# so the destination has a single source of truth. servicer.py sends the
# resulting TransferRequest to the gateway (held until the acknowledgment
# turn's audio finishes playing), and the gateway executes it over ESL
# (uuid_transfer).
# Same condition/mechanics split as _END_CALL_CONDITION_DEFAULT above:
# the condition clause is per-agent configurable (agents.transfer_prompt),
# the token mechanics (with the config-sourced destination) are fixed.
_TRANSFER_CONDITION_DEFAULT = (
    "If the caller explicitly asks to speak to a human agent or "
    "representative"
)


def _build_transfer_instruction(
    condition: str | None, transfer_type: str, destination: str,
    scripted: bool = False,
) -> str:
    """scripted=True when the agent has a configured transfer_announcement —
    same LLM-emits-only-the-token treatment as _build_end_call_instruction."""
    cond = (condition or "").strip().rstrip(".,;") or _TRANSFER_CONDITION_DEFAULT
    token = (
        f'[[TRANSFER type="{transfer_type}" destination="{destination}" '
        'reason="caller_requested_human"]]'
    )
    if scripted:
        return (
            f"\n\n{cond}, reply with ONLY the exact token {token} and no "
            "other words — the system will play the transfer announcement "
            "itself. Never say the token out loud or explain it to the caller."
        )
    return (
        f"\n\n{cond}, briefly acknowledge that you will connect them, then "
        f"end your reply with the exact token {token} on its own, after your "
        "spoken words. Only use this token when that condition is met — "
        "never say it out loud or explain it to the caller."
    )

# Phase 5F fail-fast config validation: shapes a transfer destination may
# take. Deliberately shallow — FreeSWITCH/Kamailio own real routing; this
# only catches obviously-broken config (empty, prose, a stray URL) at
# session setup instead of mid-call.
_SIP_URI_RE = re.compile(r"^sips?:[^@\s]+@[^\s]+$", re.IGNORECASE)
_PHONE_RE   = re.compile(r"^\+?\d{2,15}$")


def transfer_destination_problem(destination: str | None) -> str | None:
    """None when the destination looks routable; otherwise a human-readable
    diagnosis for the session-setup error log."""
    if destination is None or not destination.strip():
        return "transfer_destination is empty"
    d = destination.strip()
    if d.lower().startswith(("sip:", "sips:")):
        if not _SIP_URI_RE.match(d):
            return f"malformed SIP URI {d!r} (expected sip:user@host)"
        return None
    if not _PHONE_RE.match(d):
        return (
            f"transfer_destination {d!r} is neither a phone number/extension "
            "(2-15 digits, optional leading +) nor a sip:/sips: URI"
        )
    return None

# Phase 5C of AI-to-human transfer: when a cold transfer fails, generate a
# brief apology and continue the conversation rather than ending the call
# (see on_transfer_failed()). AgentRuntime (the LLM call below) receives a
# structured system event — {"type": "system_event", "event":
# "transfer_failed", "reason": ...} — embedded verbatim in a short
# natural-language wrapper (a 3B local model given bare JSON with no framing
# reliably fails to respond sensibly to it). This is appended to the LLM's
# input for one turn only — never stored in history, same "augment this
# turn's content, don't pollute future turns" treatment RAG context already
# gets (see on_speech_ended's messages_for_llm comment) — so it can't recur
# or confuse a later turn. Deliberately not a change to _system_prompt/
# _END_CALL_INSTRUCTION: the agent's fixed personality/instructions are
# untouched — the LLM generates the actual wording using its existing
# prompt, never a hardcoded recovery sentence (that only exists as
# _TRANSFER_FAILED_FALLBACK below, for the LLM-produced-nothing case).
def _build_transfer_failed_system_event(reason: str) -> str:
    event = {"type": "system_event", "event": "transfer_failed", "reason": reason or "unknown"}
    return (
        f"{json.dumps(event)}\n\n"
        "The system event above means the attempted transfer to a human agent "
        "failed. Apologize briefly to the caller for not being able to connect "
        "them to an agent, then continue assisting them with their original "
        "request."
    )

# Fixed fallback apology if the LLM produces no usable text at all (mirrors
# _FALLBACK_GOODBYE's "never leave dead air" reasoning above).
_TRANSFER_FAILED_FALLBACK = "I'm sorry, I couldn't connect you to an agent right now."


class PipelineConversationHandler:
    """
    IConversationHandler implementation that chains ISTT → ILLM → ITTS.

    on_audio()        — no-op (audio accumulation is done by ConversationSession).
    on_speech_ended() — runs the full pipeline and yields HandlerResponse items.
    on_cancel()       — signals in-flight generation to stop.
    on_session_end()  — cleans up session history.

    Takes exactly two configuration objects — runtime_config (immutable for
    the lifetime of the session; see libs.config_sdk.RuntimeConfig) and
    provider_bundle (live STT/LLM/TTS instances; see provider_bundle.py) —
    plus session-level values that aren't "configuration" at all (transcript
    persistence, this call's own tenant/call/direction identifiers from the
    gateway's SessionOpenRequest). Every field read out of runtime_config
    happens once, here, at construction — nothing later in this class ever
    calls back into the Config SDK.

    knowledge (libs.knowledge_sdk.IKnowledgeProvider) is optional and kept
    deliberately separate from runtime_config/RuntimeConfig — retrieval is
    query-dependent, not resolved once per session, and has its own
    failure mode (no eligible KB is a normal, cheap None). When set,
    on_speech_ended() makes exactly one retrieve() call per user turn (see
    that method) — never zero, never more than one. When None (or when the
    agent has no enabled KB), behavior is identical to before this feature
    existed — the "backward compatible, zero added latency for non-RAG
    agents" requirement this was built under.

    sample_rate — output PCM sample rate sent to the gateway (must match
                  the gateway's MediaConfig; a pipeline-wide constant, not
                  a per-tenant setting — see PipelineConfig).
    max_history — number of past (user, assistant) turn pairs to keep in the
                  LLM context window (also pipeline-wide, not per-tenant).
    """

    def __init__(
        self,
        runtime_config: RuntimeConfig,
        provider_bundle: ProviderBundle,
        sample_rate:   int = 16_000,
        max_history:   int = 10,
        default_system_prompt: str = "",
        transcripts:   TranscriptBuilder | None = None,
        tenant_id:     str = "",
        call_id:       str = "",
        direction:     str = "inbound",
        caller_number: str = "",
        called_number: str = "",
        knowledge:     IKnowledgeProvider | None = None,
        metrics:       IMetrics | None = None,
        tool_orchestrator: ToolCallOrchestrator | None = None,
    ) -> None:
        # default_system_prompt is a pipeline-wide fallback (PipelineConfig.
        # llm.system), not part of RuntimeConfig — it's what a resolved
        # agent with no custom system_prompt of its own gets, same "or
        # cfg.llm.system" behavior this class had before this refactor.
        self._stt          = provider_bundle.stt
        self._llm          = provider_bundle.llm
        self._tts          = provider_bundle.tts
        self._sample_rate  = sample_rate
        self._max_history  = max_history
        self._greeting     = runtime_config.conversation.greeting
        self._transcripts  = transcripts
        self._tenant_id    = tenant_id
        self._call_id      = call_id
        self._direction     = direction
        self._caller_number = caller_number
        self._called_number = called_number
        # Empty id / version 0 is the legacy-fallback adapter's honest "no
        # real Postgres row backs this" signal (see agent_config.py's
        # to_runtime_config()) — `or None` here is what keeps
        # TranscriptBuilder.begin_call() passing agent_id=None for a
        # legacy-path call, exactly as before this refactor. calls.agent_id
        # is a UUID FK; a fake non-UUID sentinel string would break the
        # insert outright, not just be cosmetically wrong.
        self._agent_id             = runtime_config.agent.id or None
        self._agent_config_version = runtime_config.version or None
        # Overrides llm.system when non-empty.  Append the end-call marker
        # instruction here (not conditionally later) so every agent gets the
        # capability regardless of whether it has a custom system_prompt.
        # Scripted spoken lines (agents.farewell_message/transfer_announcement)
        # — when set, pipeline synthesizes these verbatim at end-call/transfer
        # and the injected instructions tell the LLM to emit only the token.
        self._farewell_message = (
            (runtime_config.conversation.farewell_message or "").strip() or None
        )
        self._transfer_announcement = (
            (runtime_config.conversation.transfer_announcement or "").strip() or None
        )
        system_prompt = runtime_config.conversation.system_prompt or default_system_prompt
        self._system_prompt = (
            system_prompt
            + _build_current_date_context()
            + _build_end_call_instruction(
                runtime_config.conversation.end_call_prompt,
                scripted=self._farewell_message is not None,
            )
        ) if system_prompt else ""
        self._goodbye_grace_period_ms = runtime_config.policies.goodbye_grace_ms
        # Escalation config for record_guardrail_violation() — see that
        # method. transfer_type defaults to "none" (the column default;
        # Policies dataclass mirrors it) when an operator sets
        # escalation_threshold without configuring a real transfer
        # destination — that misconfiguration is surfaced honestly in the
        # published event rather than guessed around.
        self._transfer_type_default        = runtime_config.policies.transfer_type
        self._transfer_destination_default = runtime_config.policies.transfer_destination
        self._escalation_threshold         = runtime_config.policies.escalation_threshold
        # Caller-ID resolution inputs for _decision_context() — see
        # transfer_engine.py's _resolve_caller_id(). caller_number is the
        # constructor's own caller_number param (the caller's real ANI,
        # already stored as self._caller_number above), not a RuntimeConfig
        # field.
        self._caller_id_policy  = runtime_config.policies.caller_id_policy
        self._platform_did      = runtime_config.policies.platform_did
        self._custom_caller_id  = runtime_config.policies.custom_caller_id
        self._waiting_experience = runtime_config.policies.transfer_waiting_experience
        # Phase 5F fail-fast validation: a broken transfer config is
        # diagnosed loudly HERE, at session setup, and the trigger
        # instruction is not injected (the call proceeds AI-only) — never a
        # mid-call surprise, never a rejected call. Recovery behavior is
        # unchanged: escalation-path defaults stay as configured and are
        # surfaced honestly in published events (see
        # record_guardrail_violation).
        self._transfer_timeout_ms = validate_transfer_timeout_ms(
            runtime_config.policies.transfer_timeout_ms,
            context=f"agent={runtime_config.tenant.slug}/{runtime_config.agent.slug}",
        )
        tt = self._transfer_type_default
        if tt and tt not in ("none", "cold", "warm"):
            log.error(
                "Invalid transfer_type=%r for agent %s — treating as 'none'; "
                "fix the agent's Escalation config",
                tt, runtime_config.agent.slug,
            )
        elif tt and tt != "none":
            problem = transfer_destination_problem(self._transfer_destination_default)
            if problem:
                log.error(
                    "Transfer misconfigured for agent %s (transfer_type=%s): %s "
                    "— transfer trigger disabled for this session; fix the "
                    "agent's Escalation config",
                    runtime_config.agent.slug, tt, problem,
                )
            elif self._system_prompt:
                # Auto-inject the transfer trigger instruction — same "append
                # to the prompt here, not conditionally later" treatment as
                # _END_CALL_INSTRUCTION above, and same gate on a non-empty
                # base prompt. Config validated above: a transfer the LLM can
                # request but nothing can complete would strand callers
                # mid-"connecting you now".
                self._system_prompt += _build_transfer_instruction(
                    runtime_config.conversation.transfer_prompt,
                    tt,
                    self._transfer_destination_default,
                    scripted=self._transfer_announcement is not None,
                )
        # tenant.slug/agent.slug are real and non-empty on both the
        # Config-SDK-backed path and the legacy YAML-fallback path (see
        # agent_config.to_runtime_config()) — unlike agent.id/version,
        # there's no FK-correctness reason to sentinel these, so knowledge
        # lookups work the same way regardless of which path resolved this
        # handler.
        self._tenant_slug = runtime_config.tenant.slug
        self._agent_slug  = runtime_config.agent.slug
        self._knowledge = knowledge
        # Phase 5D: post-call cleanup after a successful transfer — see
        # session_finalizer.py. Shares this handler's own transcripts/
        # metrics sinks rather than taking a separate SessionFinalizer
        # instance, so there's one source of truth for where those go.
        self._session_finalizer = SessionFinalizer(transcripts, metrics)
        self._metrics = metrics if metrics is not None else NullMetrics()
        # Tool Execution Framework: optional, same backward-compatible
        # posture as knowledge above — None means _llm_to_tts calls
        # self._llm.generate() directly, identical to before this feature
        # existed. When set, ToolCallOrchestrator.run_turn() itself
        # degrades to the same plain-generate behavior for any agent with
        # no tools enabled (see policy_resolver.py) — the only added cost
        # is one cached (30s TTL) policy lookup per turn, not a full
        # tool-calling round trip, for a non-tool agent.
        self._tool_orchestrator = tool_orchestrator
        # Phase 6: the single arbiter of *when* to transfer — see
        # transfer_engine.py. Stateless; this handler still owns all
        # per-session state it reads (guardrail count) and produces
        # (_pending_transfer, _transfer_requested below).
        self._transfer_engine = TransferDecisionEngine(self._metrics)
        self._guardrail_counter = GuardrailCounter()
        # Per-session state — keyed by session_id.
        self._history:   dict[str, list[ChatMessage]] = {}
        self._cancelled: dict[str, asyncio.Event]     = {}
        self._pending_transfer:  dict[str, TransferRequest] = {}
        # Duplicate-suppression bookkeeping the engine itself deliberately
        # doesn't keep (see DecisionContext.already_requested docstring) —
        # marked once an accepted Decision has been produced for a session,
        # cleared at session end. Not a second FSM: this only ever answers
        # "has *a* transfer already been requested this session," it has no
        # notion of Transferring vs Finalizing vs Closed (those states live
        # in ConversationFSM, which this handler has no visibility into).
        self._transfer_requested: set[str] = set()
        # Phase 5C: transfer-failure-recovery turns, recorded in short-term
        # memory (self._history) immediately, but NOT persisted to
        # transcripts until on_session_end — see on_transfer_failed() and
        # requirement "persist only during normal SessionEnd."
        self._pending_recovery_turns: dict[str, list[tuple[str, str, bool]]] = {}
        # Tool-call filler (_TOOL_CALL_FILLER) fires at most once per CALL,
        # not once per turn — confirmed live as a real bug otherwise: a
        # multi-turn booking conversation (try book -> ask for phone number
        # -> retry book) calls a tool on nearly every turn, and repeating
        # "Let me check that for you" each time sounded broken/annoying to
        # a real caller. Keyed by session_id, matching
        # _pending_recovery_turns's own per-session-on-one-handler pattern.
        self._filler_spoken_for_session: set[str] = set()

    # ── IConversationHandler ───────────────────────────────────────────────────

    async def greeting(self, session_id: str) -> list[bytes]:
        if self._transcripts is not None:
            self._transcripts.begin_call(
                session_id, self._tenant_id, self._call_id,
                self._direction, self._caller_number, self._called_number,
                self._agent_id, self._agent_config_version,
            )
        if not self._greeting:
            return []
        return [chunk async for chunk in self._synthesize_sentence_stream(self._greeting, session_id)]

    async def on_audio(self, session_id: str, payload: bytes) -> HandlerResponse:
        # Audio is also accumulated by ConversationSession (still the source
        # of truth passed to on_speech_ended) — but forward every chunk to
        # the STT provider immediately too, so a genuinely streaming
        # provider (Deepgram) can transcribe continuously instead of
        # waiting for the whole utterance. A no-op for a provider with no
        # live stream (FasterWhisperSTT) — see ISTT.feed_stream's docstring.
        try:
            await self._stt.feed_stream(session_id, payload, self._sample_rate)
        except Exception:
            log.exception("STT feed_stream failed session=%s", session_id)
        return HandlerResponse()

    async def on_speech_ended(
        self,
        session_id:  str,
        audio:       bytes,
        duration_ms: int,
        energy_db:   float,
    ) -> AsyncGenerator[HandlerResponse, None]:
        # Fresh per-turn cancel event, replaced before any await.  Never carry
        # over a set event: a cancel targets the response in flight when it was
        # issued, not this new utterance.
        cancel_event = asyncio.Event()
        self._cancelled[session_id] = cancel_event

        # Sub-1s blips (echo tails, breaths, ambient noise) make Whisper
        # hallucinate filler or, worse, guess a wrong language entirely on
        # noise with no real content (observed live: a noise blip
        # transcribed as Turkish/Telugu at 50-70% confidence, which then
        # fed a hallucinated LLM response) — 300ms was too permissive.
        # 1000ms matches the threshold validated in an earlier prototype
        # of this pipeline.
        min_bytes = int(1.0 * self._sample_rate) * 2  # 1000 ms of S16LE mono
        if len(audio) < min_bytes:
            log.debug(
                "Skipping short utterance (%d bytes < %d) session=%s",
                len(audio), min_bytes, session_id,
            )
            return

        # Voice-to-voice turn latency instrumentation — see
        # transcript_builder.py's record_turn(); the schema already had
        # stt_latency_ms/llm_latency_ms/tts_latency_ms columns scaffolded
        # since Phase 6a, never actually populated until now. turn_start is
        # the caller's own reference point (end of their speech), the
        # number that actually matters for perceived responsiveness — not
        # any single stage's internal timing.
        turn_start = time.monotonic()

        # ── 1. STT ─────────────────────────────────────────────────────────────
        stt_t0 = time.monotonic()
        try:
            stt_result: SttResult = await self._stt.finalize_stream(session_id, audio, self._sample_rate)
        except Exception:
            log.exception("STT failed session=%s", session_id)
            return
        stt_ms = (time.monotonic() - stt_t0) * 1000

        if not stt_result.text or cancel_event.is_set():
            log.debug("STT empty or cancelled session=%s", session_id)
            return

        log.info("STT result=%r session=%s", stt_result.text, session_id)
        yield HandlerResponse(stt_text=stt_result.text, stt_confidence=stt_result.confidence)

        # Deterministic, inline caller-frustration/abuse signal (see
        # guardrails.py) — the "real detector" record_guardrail_violation()
        # was built to receive. Runs on the transcript already in hand: no
        # LLM call, no network, no media-path cost. Always counted/logged
        # for observability even when escalation_threshold is unset; only
        # actually escalates per that agent's Escalation config. A breach
        # here doesn't yield yet — record_guardrail_violation() just stores
        # a pending TransferRequest, which this same on_speech_ended() call
        # surfaces near its end (see the transfer_request block below): the
        # agent still finishes responding to this utterance normally, then
        # the transfer follows right after, same as an LLM-emitted directive.
        violation = GuardrailDetector.check(stt_result.text)
        if violation is not None:
            log.info(
                "Guardrail violation category=%s matched=%r session=%s",
                violation.category, violation.matched, session_id,
            )
            self.record_guardrail_violation(session_id)
        else:
            # Consecutive counter (per Phase 6 spec): a turn that wasn't
            # flagged resets the streak, so a caller frustrated once, then
            # satisfied, then frustrated again starts counting from 1
            # rather than accumulating across the whole call.
            self._guardrail_counter.reset(session_id)

        # ── 2. LLM ─────────────────────────────────────────────────────────────
        history = self._get_history(session_id)
        # Prepend per-agent system prompt as the first message if configured.
        if self._system_prompt and not history:
            history.append(ChatMessage(role="system", content=self._system_prompt))
        history.append(ChatMessage(role="user", content=stt_result.text))

        # ── Knowledge retrieval — exactly one call per turn, never zero,
        # never more than one. Folded into this turn's own user-message
        # content (never appended to `history` itself, so it doesn't
        # inflate context on future turns) — deliberately NOT sent as a
        # second system-role message. Ollama/chat-tuned models are trained
        # on a single leading system message; a system message reappearing
        # mid-conversation is out-of-distribution and measurably degrades
        # adherence to the *first* system message's instructions (notably
        # the end-call marker — see _END_CALL_INSTRUCTION), since recency
        # bias pulls attention toward whichever system-role turn is closer
        # to generation. Prefixing the retrieved context onto the user
        # turn is the standard, safe RAG prompting pattern and leaves
        # exactly one system message in the conversation, always.
        messages_for_llm = history
        if self._knowledge is not None:
            context = await self._retrieve_context(stt_result.text, session_id)
            if context is not None and context.chunks:
                augmented = ChatMessage(
                    role="user",
                    content=f"{self._format_context(context)}\n\nCaller's question: {stt_result.text}",
                )
                messages_for_llm = history[:-1] + [augmented]

        directives: list[Directive] = []
        full_response: list[str] = []
        end_call = False
        any_audio = False
        llm_t0 = time.monotonic()
        first_token_at: float | None = None
        first_audio_at: float | None = None
        try:
            async for chunk, tts_audio, marker_seen in self._llm_to_tts(
                messages_for_llm, cancel_event, session_id, directives
            ):
                now = time.monotonic()
                if first_token_at is None:
                    first_token_at = now
                full_response.append(chunk)
                end_call = marker_seen
                if tts_audio:
                    if first_audio_at is None:
                        first_audio_at = now
                    any_audio = True
                    yield HandlerResponse(tts_payloads=[tts_audio])
                if cancel_event.is_set():
                    break
        except Exception:
            log.exception("LLM/TTS pipeline failed session=%s", session_id)

        # llm_ms: time to the LLM's first token/event — the "thinking" time
        # a caller actually experiences before anything happens. tts_ms:
        # time from that first token to the first synthesized sentence
        # actually being ready — the two are sequential buckets, not
        # overlapping, so they sum toward voice_to_voice_ms below (measured
        # directly too, rather than trusted as a derived sum, since a
        # cancelled/errored turn can leave either timestamp unset).
        llm_ms = (first_token_at - llm_t0) * 1000 if first_token_at else None
        tts_ms = (first_audio_at - first_token_at) * 1000 if (first_audio_at and first_token_at) else None
        voice_to_voice_ms = (first_audio_at - turn_start) * 1000 if first_audio_at else None

        # Directives are already stripped mid-stream (see _llm_to_tts) for
        # whatever actually reached TTS; re-parsing here covers the same
        # ground for the raw joined tokens kept in full_response, which is
        # otherwise unstripped (see _llm_to_tts's docstring: `chunk` is the
        # raw token, not the cleaned text).
        assistant_text = DirectiveParser.parse("".join(full_response)).clean_text.strip()

        if full_response and not cancel_event.is_set():
            # A cancelled response (≥1 token, interrupted) is treated as zero
            # tokens: discard it so history only ever holds complete pairs.
            # Marker stripped here too — full_response is raw per-token text, so
            # a marker split across tokens is only contiguous once joined, and
            # it must never leak into history or get referenced in a later turn.
            history.append(ChatMessage(role="assistant", content=assistant_text))
            self._trim_history(session_id)
        else:
            # Barge-in, cancel, or LLM failure: remove the unpaired user message
            # so history stays consistent for the next turn's LLM context.
            if history and history[-1].role == "user":
                history.pop()

        if self._transcripts is not None:
            self._transcripts.record_turn(
                session_id, stt_result.text, stt_result.confidence,
                assistant_text, cancel_event.is_set(),
                latency=TurnLatency(
                    stt_ms=stt_ms, llm_ms=llm_ms, tts_ms=tts_ms, voice_to_voice_ms=voice_to_voice_ms,
                    stt_engine=type(self._stt).__name__, llm_engine=type(self._llm).__name__,
                    tts_engine=type(self._tts).__name__,
                ),
            )

        # Signal the servicer to end the call once this turn's audio has
        # streamed — but not if the caller barged in (cancel_event.is_set()):
        # them talking means the agent's decision to end the call is stale.
        if end_call and not cancel_event.is_set():
            got_farewell_audio = False
            if self._farewell_message:
                # Scripted farewell — spoken verbatim as the turn's closing
                # audio. The injected instruction told the LLM to reply with
                # only the token, so this is normally the only speech; if the
                # model spoke anyway, the script still plays after it
                # (deterministic wording beats deduplication here).
                async for chunk in self._synthesize_sentence_stream(self._farewell_message, session_id):
                    got_farewell_audio = True
                    yield HandlerResponse(tts_payloads=[chunk])
            if not any_audio and not got_farewell_audio:
                # Marker-only reply with no scripted line (or its synthesis
                # failed): synthesize a fallback so the servicer actually
                # has audio to key EndCall off of.
                async for chunk in self._synthesize_sentence_stream(_FALLBACK_GOODBYE, session_id):
                    yield HandlerResponse(tts_payloads=[chunk])
            yield HandlerResponse(
                end_call=True,
                end_call_grace_period_ms=self._goodbye_grace_period_ms,
            )

        # Phase 6: an LLM-emitted directive takes precedence over a pending
        # escalation-threshold trigger from an earlier turn — either way,
        # at most one TransferRequest is yielded per turn. This ordering
        # (check the directive first; only fall back to a pending
        # escalation trigger if there wasn't one) is orchestration the
        # engine itself is deliberately not asked to arbitrate — see
        # transfer_engine.py's module docstring: it evaluates one trigger
        # at a time and stays a pure function, so "which trigger wins when
        # two fire the same turn" is this caller's own sequencing, not
        # engine policy. A caller barge-in during this turn
        # (cancel_event.is_set()) makes the agent's decision stale, same
        # reasoning as end_call above.
        if not cancel_event.is_set():
            transfer_directive = next(
                (d for d in directives if isinstance(d, TransferDirective)), None,
            )
            transfer_request: TransferRequest | None = None
            if transfer_directive is not None:
                decision = self._transfer_engine.evaluate(
                    self._decision_context(session_id),
                    TransferTrigger(type=TriggerType.LLM_DIRECTIVE, directive=transfer_directive),
                )
                if decision.accepted:
                    transfer_request = decision.request
                    self._transfer_requested.add(session_id)
            # Fall through to a pending escalation-accepted request whenever
            # the directive path produced nothing — including when a
            # directive WAS emitted but the engine rejected it as
            # already_transferring precisely BECAUSE that pending request
            # exists. Without this, the accepted pending transfer would be
            # starved for as long as the LLM keeps re-emitting directives.
            if transfer_request is None:
                transfer_request = self._pending_transfer.pop(session_id, None)

            if transfer_request is not None:
                if self._transfer_announcement:
                    # Scripted announcement — yielded as this turn's TTS so
                    # the servicer holds the TransferRequest until it has
                    # actually played (see servicer.py's pending_transfer),
                    # exactly like an LLM-spoken acknowledgment would be.
                    async for chunk in self._synthesize_sentence_stream(self._transfer_announcement, session_id):
                        yield HandlerResponse(tts_payloads=[chunk])
                yield HandlerResponse(transfer_request=transfer_request)

    async def on_cancel(self, session_id: str) -> None:
        self._cancel_event(session_id).set()

    async def on_session_end(self, session_id: str, reason: str,
                             final_state: str | None = None) -> None:
        # Requirement: transfer-failure recovery turns are recorded in
        # short-term memory (history) immediately, in on_transfer_failed(),
        # but their transcript *persistence* is deferred until now — normal
        # SessionEnd — rather than written immediately like every other
        # turn's record_turn() call.
        if self._transcripts is not None:
            for caller_text, ai_response, interrupted in self._pending_recovery_turns.pop(session_id, []):
                self._transcripts.record_turn(session_id, caller_text, 1.0, ai_response, interrupted)
            self._transcripts.end_call(session_id, reason, final_state=final_state)
        else:
            self._pending_recovery_turns.pop(session_id, None)
        self._history.pop(session_id, None)
        self._cancelled.pop(session_id, None)
        self._filler_spoken_for_session.discard(session_id)
        self._guardrail_counter.forget(session_id)
        self._pending_transfer.pop(session_id, None)
        self._transfer_requested.discard(session_id)
        try:
            await self._stt.cancel_stream(session_id)
        except Exception:
            log.exception("STT cancel_stream failed session=%s", session_id)

    def _decision_context(self, session_id: str) -> DecisionContext:
        return DecisionContext(
            session_id=session_id, tenant_id=self._tenant_id, call_id=self._call_id,
            transfer_type=self._transfer_type_default,
            transfer_destination=self._transfer_destination_default,
            escalation_threshold=self._escalation_threshold,
            already_requested=session_id in self._transfer_requested,
            caller_id_policy=self._caller_id_policy,
            platform_did=self._platform_did,
            custom_caller_id=self._custom_caller_id,
            caller_number=self._caller_number,
            waiting_experience=self._waiting_experience,
        )

    def record_guardrail_violation(self, session_id: str) -> TransferRequest | None:
        """
        Increments this session's consecutive guardrail-violation counter
        (see guardrails.GuardrailCounter) and asks the TransferDecisionEngine
        (Phase 6) whether that warrants a transfer. When accepted, stores
        the resulting TransferRequest for on_speech_ended() to surface via
        HandlerResponse — at the end of the *current* turn when called
        inline from the guardrail check there, or on the next turn when
        called externally between turns. Same publish path as an
        LLM-emitted [[TRANSFER]] directive.

        The counting/threshold-comparison split is deliberate (see
        transfer_engine.py's module docstring): this method owns "was this
        a violation, what's the count," the engine owns "given this count,
        should we transfer." escalation_threshold=None (the column's
        default) means escalation is disabled: violations are still
        counted but the engine never accepts, so a caller can call this
        unconditionally without checking whether escalation is configured.
        """
        count = self._guardrail_counter.increment(session_id)
        decision = self._transfer_engine.evaluate(
            self._decision_context(session_id),
            TransferTrigger(type=TriggerType.ESCALATION, violation_count=count),
        )
        if not decision.accepted:
            return None
        self._transfer_requested.add(session_id)
        self._pending_transfer[session_id] = decision.request
        return decision.request

    async def on_transfer_failed(
        self, session_id: str, destination: str, reason: str,
    ) -> AsyncGenerator[HandlerResponse, None]:
        """
        Phase 5C of AI-to-human transfer: a cold transfer failed (see
        TransferFailed in the gRPC protocol) — generate a brief apology
        through the same LLM->TTS pipeline every other turn uses, and
        continue the conversation rather than ending the call. No STT step
        (nothing was said this "turn") and no permanent user-role message
        is added to history — only the ephemeral structured system event
        below (see _build_transfer_failed_system_event and the module
        comment above it).

        Directives are still detected/stripped the normal way (see
        _llm_to_tts) but not specially interpreted here — if the LLM
        somehow emits [[END_CALL]] or [[TRANSFER]] in its apology, that's
        treated as this turn's own business, same as any other turn's
        response; this method does not suppress or encourage that.
        """
        cancel_event = asyncio.Event()
        self._cancelled[session_id] = cancel_event

        # This attempt is over (unsuccessfully) — the call continues, so a
        # caller who asks again must get a fresh TransferDecisionEngine
        # evaluation, not an "already_transferring" rejection.
        self._transfer_requested.discard(session_id)
        # The speculative summary started on TransferInitiated (see
        # start_finalization()) was for a call that was about to end — it
        # isn't, so throw it away rather than let it run unattended.
        self._session_finalizer.discard_pending_summary(session_id)

        history = self._get_history(session_id)
        if self._system_prompt and not history:
            history.append(ChatMessage(role="system", content=self._system_prompt))

        # AgentRuntime (the LLM call below) receives the structured system
        # event — see _build_transfer_failed_system_event's doc comment for
        # why it's wrapped rather than sent as bare JSON.
        notice = ChatMessage(
            role="user",
            content=_build_transfer_failed_system_event(reason),
        )
        messages_for_llm = history + [notice]

        directives: list[Directive] = []
        full_response: list[str] = []
        any_audio = False
        try:
            async for chunk, tts_audio, _end_call in self._llm_to_tts(
                messages_for_llm, cancel_event, session_id, directives
            ):
                full_response.append(chunk)
                if tts_audio:
                    any_audio = True
                    yield HandlerResponse(tts_payloads=[tts_audio])
                if cancel_event.is_set():
                    break
        except Exception:
            log.exception("Transfer-failure recovery LLM/TTS pipeline failed session=%s", session_id)

        assistant_text = DirectiveParser.parse("".join(full_response)).clean_text.strip()

        if not assistant_text and not any_audio and not cancel_event.is_set():
            # LLM produced nothing usable — never leave dead air, same
            # reasoning as _FALLBACK_GOODBYE above.
            assistant_text = _TRANSFER_FAILED_FALLBACK
            async for chunk in self._synthesize_sentence_stream(assistant_text, session_id):
                yield HandlerResponse(tts_payloads=[chunk])

        # Recorded as a normal assistant turn (memory) — no matching
        # "user" turn is stored, same asymmetry RAG-augmented turns already
        # tolerate; the ephemeral notice above is never persisted.
        if assistant_text and not cancel_event.is_set():
            history.append(ChatMessage(role="assistant", content=assistant_text))
            self._trim_history(session_id)

        # Requirement: do not persist memory immediately — buffer this turn
        # and only write it during normal SessionEnd (see on_session_end()).
        # Short-term memory (self._history above) already happened; this is
        # about deferring the transcript *persistence* specifically.
        self._pending_recovery_turns.setdefault(session_id, []).append((
            f"[transfer_failed: {reason}]", assistant_text, cancel_event.is_set(),
        ))

    def on_transfer_cancelled(self, session_id: str) -> None:
        """A pending transfer was dropped before dispatch (caller barge-in
        during the acknowledgment — see ConversationSession.
        on_transfer_cancelled). Release the duplicate-suppression flag: the
        gateway never received this attempt, so a caller who barges in and
        then asks again must get a fresh TransferDecisionEngine evaluation,
        not an "already_transferring" rejection."""
        self._transfer_requested.discard(session_id)
        self._session_finalizer.discard_pending_summary(session_id)

    def start_finalization(self, session_id: str) -> None:
        """Called on TransferInitiated (see session.py) — see
        session_finalizer.py's start_summary_early() for why this exists
        and what it does and does not start early."""
        self._session_finalizer.start_summary_early(
            session_id, self._get_history(session_id), self._llm,
        )

    async def finalize_session(
        self, session_id: str, reason: str = "transfer_completed",
    ) -> FinalizationResult:
        """
        Phase 5D of AI-to-human transfer: runs SessionFinalizer's post-call
        cleanup pipeline after a successful transfer (see
        session_finalizer.py), using this handler's own per-session state
        (history, LLM instance, cancel event) — the same reason
        ConversationSession can't run this itself: it has no access to
        that private state, only to whatever IConversationHandler protocol
        methods expose. Returns the full result (summary + whether it was
        really generated vs. a timeout fallback + whether it was
        persisted) so the caller can build an accurate ConversationFinalized
        message (see session.py).
        """
        return await self._session_finalizer.finalize(
            session_id,
            self._get_history(session_id),
            self._llm,
            self._cancel_event(session_id),
            reason,
        )

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _token_stream(
        self, history: list[ChatMessage], session_id: str, cancel_event: asyncio.Event,
    ) -> AsyncGenerator[str | ToolCallStartedEvent, None]:
        """Token stream, identical shape to self._llm.generate() except for
        one addition — the seam that makes the rest of _llm_to_tts (sentence
        splitting, directive parsing) mostly unaware tool-calling exists.
        ToolCallOrchestrator.run_turn() yields TokenEvent (unwrapped to
        .text, keeping exactly the per-sentence TTS latency it has today)
        and, once per tool call, a single ToolCallStartedEvent passed
        through as-is so _llm_to_tts can speak a filler before the (a
        ToolCallEvent itself is consumed internally, folded into `history`,
        and never surfaces here) potentially slow round-trip (see design
        §12's state-transition note: the pause is real, but previously had
        no caller-facing signal at all)."""
        if self._tool_orchestrator is None:
            async for token in self._llm.generate(history):
                yield token
            return

        async for event in self._tool_orchestrator.run_turn(
            self._agent_id or "", self._tenant_id, self._call_id, session_id, history,
            caller_number=self._caller_number, cancel_event=cancel_event,
        ):
            if isinstance(event, ToolCallStartedEvent):
                yield event
                continue
            assert isinstance(event, ToolTokenEvent)
            yield event.text

    async def _llm_to_tts(
        self,
        history:      list[ChatMessage],
        cancel_event: asyncio.Event,
        session_id:   str,
        directives:   list[Directive],
    ) -> AsyncGenerator[tuple[str, bytes, bool], None]:
        """
        Stream LLM tokens, buffer into sentences, synthesise each sentence.
        Yields (token, tts_bytes, end_call) triples; tts_bytes is empty
        until a sentence is ready. `token` is the raw LLM token (may
        contain directive-tag fragments — see on_speech_ended's
        assistant_text, which re-parses the joined raw tokens). end_call
        becomes True once an EndCallDirective is found and stays True for
        the rest of this turn. Every directive found this turn is appended
        to the caller-supplied `directives` list (mutated in place, so the
        caller sees them without changing this generator's yield shape).

        Directive tags ([[END_CALL]], [[TRANSFER ...]], any future kind)
        are buffered by `stream_buf` (StreamBuffer) and parsed by
        `DirectiveParser` (see directives.py) *before* the text reaches
        the sentence splitter — an unterminated "[[" is held back until
        its "]]" arrives, so a tag can never be chopped in half and
        partially spoken, and never reaches TTS at all.
        """
        stream_buf  = StreamBuffer()
        text_buffer = ""  # directive-free text awaiting a sentence boundary
        try:
            async for item in self._token_stream(history, session_id, cancel_event):
                if cancel_event.is_set():
                    break
                if isinstance(item, ToolCallStartedEvent):
                    # At most once per CALL (not per turn) — see
                    # _filler_spoken_for_session's own comment for why.
                    if session_id not in self._filler_spoken_for_session:
                        self._filler_spoken_for_session.add(session_id)
                        any_filler_chunk = False
                        async for chunk in self._synthesize_sentence_stream(_TOOL_CALL_FILLER, session_id):
                            if not any_filler_chunk:
                                any_filler_chunk = True
                                log.info("Tool-call filler spoken tool=%s session=%s", item.tool_name, session_id)
                            yield "", chunk, False
                    continue
                token = item
                result = DirectiveParser.parse(stream_buf.feed(token))
                directives.extend(result.directives)
                text_buffer += result.clean_text
                end_call = any(isinstance(d, EndCallDirective) for d in directives)
                if result.directives:
                    for d in result.directives:
                        if isinstance(d, EndCallDirective):
                            log.info("End-call marker detected session=%s", session_id)
                yield token, b"", end_call

                # Split on sentence boundaries and synthesise each complete sentence.
                while True:
                    parts = _SENTENCE_RE.split(text_buffer, maxsplit=1)
                    if len(parts) < 2:
                        break
                    sentence, text_buffer = parts[0].strip(), parts[1]
                    if sentence:
                        async for chunk in self._synthesize_sentence_stream(sentence, session_id):
                            yield "", chunk, end_call
        except Exception:
            log.exception("LLM streaming failed session=%s", session_id)
            if not cancel_event.is_set():
                fallback_text = _FALLBACK_LLM_ERROR
                async for chunk in self._synthesize_sentence_stream(_FALLBACK_LLM_ERROR, session_id):
                    yield fallback_text, chunk, False
                    fallback_text = ""  # yielded once — see full_response.append(chunk) at the call site

        # Whatever StreamBuffer still has pending never closed into a
        # complete tag — a false-positive lookalike (the model literally
        # said "[[" in prose), not a real directive. Flush it as ordinary
        # text rather than silently dropping it.
        text_buffer += stream_buf.flush()

        end_call = any(isinstance(d, EndCallDirective) for d in directives)
        if text_buffer.strip() and not cancel_event.is_set():
            async for chunk in self._synthesize_sentence_stream(text_buffer.strip(), session_id):
                yield "", chunk, end_call

    async def _retrieve_context(self, query: str, session_id: str):
        # A retrieval failure must never fail the turn — same "degrade to
        # no context, not a broken response" contract IKnowledgeProvider
        # itself already guarantees (RepositoryUnavailableError is caught
        # inside CacheAsideKnowledgeProvider), but this is defense in depth
        # against any other exception (a bad MockKnowledgeProvider in a
        # test, an unexpected bug) reaching the caller's turn.
        try:
            return await self._knowledge.retrieve(
                self._tenant_slug, self._agent_slug, query, RetrievalPolicy(),
            )
        except Exception:
            log.exception("Knowledge retrieval failed session=%s", session_id)
            return None

    @staticmethod
    def _format_context(context) -> str:
        text = "Relevant information that may help answer the caller's question:\n"
        text += "\n\n".join(chunk.content for chunk in context.chunks)
        if context.include_citations and context.sources:
            text += "\n\nSources: " + ", ".join(context.sources)
        return text

    async def _synthesize_sentence_stream(self, text: str, session_id: str) -> AsyncGenerator[bytes, None]:
        # Forwards each chunk the TTS provider yields immediately — real
        # latency win only for a provider with genuine incremental
        # synthesis (Deepgram today); a no-genuine-streaming provider
        # (macOS/Kokoro/ElevenLabs) yields its one complete result once
        # (see ITTS.synthesize_stream's docstring).
        try:
            async for chunk in self._tts.synthesize_stream(text, self._sample_rate):
                yield chunk
        except Exception:
            log.exception("TTS streaming failed text=%r session=%s", text, session_id)

    def _cancel_event(self, session_id: str) -> asyncio.Event:
        if session_id not in self._cancelled:
            self._cancelled[session_id] = asyncio.Event()
        return self._cancelled[session_id]

    def _get_history(self, session_id: str) -> list[ChatMessage]:
        if session_id not in self._history:
            self._history[session_id] = []
        return self._history[session_id]

    def _trim_history(self, session_id: str) -> None:
        history = self._history.get(session_id, [])
        # Preserve a leading system message so it is not silently sliced away
        # when the conversation grows past max_history turns.  Without this,
        # history[-N:] drops history[0] and the `if not history` guard on the
        # next turn is never True, so the system prompt is never re-injected.
        base = 1 if (history and history[0].role == "system") else 0
        max_msgs = self._max_history * 2 + base
        if len(history) > max_msgs:
            self._history[session_id] = history[:base] + history[-self._max_history * 2:]
