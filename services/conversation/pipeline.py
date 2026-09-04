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
from dataclasses import dataclass, field
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
    strip_markdown_chars,
)
from .guardrails import GuardrailCounter, GuardrailDetector
from .metrics import IMetrics, NullMetrics
from .provider_bundle import ProviderBundle
from .providers.interfaces import ChatMessage, SttResult
from .session import HandlerResponse, NodeChanged
from .session_finalizer import FinalizationResult, SessionFinalizer
from .tools.llm_adapter import DeterministicSpokenEvent
from .tools.llm_adapter import LocalToolCompletedEvent
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
from .workflow import (
    ContextSummarizer,
    VariableExtractor,
    WorkflowRunner,
    graph_for,
    summary_threshold_for,
)

log = logging.getLogger(__name__)


@dataclass
class _SessionState:
    """Everything PipelineConversationHandler tracks per live session_id,
    bundled into one object instead of a dozen separate top-level dicts/
    sets each needing its own cleanup line in on_session_end(). Adding a
    new piece of per-session state means adding one field here, not a new
    dict *plus* a new pop/discard call to remember."""
    history:                         list[ChatMessage] = field(default_factory=list)
    cancelled:                       asyncio.Event = field(default_factory=asyncio.Event)
    pending_transfer:                "TransferRequest | None" = None
    transfer_requested:              bool = False
    pending_recovery_turns:          list[tuple[str, str, bool]] = field(default_factory=list)
    tool_call_filler_index:          int = 0
    tool_call_filler_last_spoken:    float | None = None
    first_turn_filler_spoken:        bool = False
    fabrication_triggered_transfer:  bool = False
    confirmed_booking_slot:          str | None = None
    phone_number_confirmed:          bool = False

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
# Fixed wording, appended to every step's prompt. It used to be per-agent
# configurable (agents.end_call_prompt), which put "when does this call end?"
# in two places: here, and the graph's end steps. The graph is the answer —
# this is only the safety net for a caller who finishes somewhere no
# connection covers, so it needs no per-agent tuning.
_END_CALL_INSTRUCTION = (
    "\n\nWhen the conversation is genuinely finished (the caller says "
    "goodbye, has no more questions, or the issue is resolved), end your "
    f"final reply with the exact token {_END_CALL_MARKER} on its own, after "
    "your spoken words. Only use this token when you are truly ending the "
    "call — never say it out loud or explain it to the caller."
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


def _build_caller_number_context(caller_number: str) -> str:
    """CalendarExecutor already defaults attendee_phone to the caller's
    real ANI (request.context.caller_number) when the LLM doesn't supply
    one explicitly — but the LLM itself never sees those digits anywhere
    in its context, so it can only ever ask for a number from scratch, the
    exact STT-mis-transcription risk this is meant to avoid (confirmed
    live: a spoken-and-mis-heard digit got confirmed and booked wrong).
    Pre-spaced digit-by-digit here, matching the digit-confirmation
    guardrail's own formatting convention, both so the instruction reads
    naturally and so the LLM's first exposure to "how to write this
    number" is already in the safe, TTS-speaks-each-digit shape.

    Empty caller_number (browser test calls, some SIP trunks that don't
    pass ANI) returns "" — prompt is unchanged, agent falls back to
    asking directly, today's existing behavior. has_booking_tool gates
    this call site so a non-booking agent never gets booking-flow
    instructions injected."""
    if not caller_number:
        return ""
    spaced = " ".join(caller_number)
    return (
        f"\n\nThe caller's phone number from Caller ID is: {spaced}. Before "
        "booking, state this number back to the caller one digit at a time "
        "(never as a compound number) and ask if it's the best number to "
        "reach them. If they confirm, use it as-is — you don't need to "
        "repeat it in the tool call. If they say it's different or wrong, "
        "ask them to say the correct number one digit at a time, including "
        "country code, then read it back the same way to confirm before "
        "you book anything. "
        "Confirmed live: a caller who doesn't actually answer that question "
        "— changes the subject, asks something else, says something "
        "unrelated — has NOT confirmed the number, even if the "
        "conversation moves on. Never treat silence or a topic change as "
        "confirmation. If you asked and never got a clear yes or a "
        "corrected number, ask again before you book anything — do not "
        "let the conversation drift into scheduling with an unconfirmed "
        "number. "
        "Confirmed live, repeatedly: once you already have a date, a time, "
        "and the caller has confirmed this number, the very next thing you "
        "do must be to actually call the booking tool — not read the "
        "number back again, not ask another question, not say anything "
        "about the appointment yourself. Confirming the same number twice "
        "in a row, or saying anything that sounds like the appointment is "
        "set, without that tool call having actually happened in this "
        "exact turn, is the single most serious mistake you can make on "
        "this call."
    )


_DIGIT_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_AFFIRMATIVE_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|correct|right|that'?s\s+(right|correct)|ok(ay)?)\b", re.IGNORECASE,
)


def _extract_spoken_digits(text: str) -> str:
    """A digit confirmation readback can spell digits as words ("eight
    nine seven") or as numerals ("8 9 7") depending on how the model
    happens to phrase it this time — normalize both to one digit string
    so the two representations compare equal."""
    out = []
    for word in re.findall(r"[A-Za-z]+|\d+", text):
        if word.isdigit():
            out.append(word)
        else:
            digit = _DIGIT_WORDS.get(word.lower())
            if digit:
                out.append(digit)
    return "".join(out)


def _message_reads_back_phone_number(text: str, caller_number: str) -> bool:
    """True when `text` (an assistant turn) appears to have just spoken
    the caller's own number back to them — the digit-confirmation moment
    _build_caller_number_context() instructs the agent to do before
    booking. Compares only the last 7 digits so country-code/leading-zero
    formatting differences between what was injected and what the model
    actually said don't cause a false negative."""
    if not caller_number:
        return False
    target = re.sub(r"\D", "", caller_number)
    if len(target) < 7:
        return False
    return target[-7:] in _extract_spoken_digits(text)


def _caller_just_confirmed_phone_number(history: list[ChatMessage], caller_number: str) -> bool:
    """The one narrow, deterministic condition worth forcing tool_choice
    over: the immediately preceding assistant turn read the caller's
    number back to them, and this turn's caller reply is a short
    affirmative — the exact moment book_appointment should be called,
    confirmed live, repeatedly, to instead sometimes get skipped entirely
    with no explanation. history[-1] is this turn's just-appended caller
    message (see on_speech_ended's own append, right before _llm_to_tts
    runs); history[-2], if present and from the assistant, is the turn
    being checked for the readback."""
    if len(history) < 2 or history[-2].role != "assistant":
        return False
    if not _AFFIRMATIVE_RE.match(history[-1].content or ""):
        return False
    return _message_reads_back_phone_number(history[-2].content or "", caller_number)


def _claim_matches_confirmed_slot(assistant_text: str, confirmed_datetime: str) -> bool:
    """True when assistant_text appears to be describing the SAME slot
    confirmed_datetime already real, truthfully — as opposed to a claim
    about some OTHER time. Confirmed live: tracking only a boolean "a
    booking succeeded at some point" let a later, genuinely different,
    never-confirmed reschedule claim through too, since the flag never
    reset. Crude but fails in the safer direction: both the day-of-month
    and the hour (12-hour or 24-hour) must appear as numerals in the
    text for a match; anything else — including a date this can't even
    parse — is treated as NOT a match, so the fabrication check below
    still runs rather than silently waving through a claim this function
    isn't sure about."""
    try:
        dt = datetime.fromisoformat(confirmed_datetime)
    except ValueError:
        return False
    digits_in_text = set(re.findall(r"\d+", assistant_text))
    day_matches = str(dt.day) in digits_in_text
    hour12 = dt.strftime("%I").lstrip("0") or "12"
    hour_matches = hour12 in digits_in_text or str(dt.hour) in digits_in_text
    return day_matches and hour_matches


_BOOKING_CLAIM_RE = re.compile(
    # rescheduled/re-booked included explicitly — confirmed live, a claim
    # about a reschedule uses these words, and \bscheduled\b alone never
    # matches "rescheduled" (no word boundary between "re" and
    # "scheduled" — both are word characters).
    r"\b(booked|rebooked|confirmed|scheduled|rescheduled|all set)\b", re.IGNORECASE,
)
_BOOKING_SUBJECT_RE = re.compile(
    r"\b(appointment|demo|booking|meeting|slot)\b", re.IGNORECASE,
)


def _claims_booking_without_tool_call(assistant_text: str) -> bool:
    """Heuristic, not a parser: a small local model can phrase a fabricated
    confirmation in unlimited ways, so this only catches the common
    "booked/confirmed/scheduled" + "appointment/demo/booking" pairing seen
    in a real live incident (see call site's comment) — a
    deliberate false-negative-tolerant backstop, not the primary fix
    (the primary fix is the LLM actually calling the tool; this only
    limits the blast radius when it doesn't)."""
    return bool(_BOOKING_CLAIM_RE.search(assistant_text) and _BOOKING_SUBJECT_RE.search(assistant_text))

# Fallback farewell synthesised when the LLM emits the end-call marker with
# no spoken text (e.g. replying to "tear down the call" with only the
# marker). Without this, tts_started_sent never flips true on the gateway
# side and the servicer silently drops EndCall (see servicer.py) — the call
# would never hang up on its own, forcing the caller to disconnect manually.
_FALLBACK_GOODBYE = "Goodbye."

# Ceiling on the workflow's own teardown work (a final extraction pass plus
# whatever background extractions are still in flight) — see
# on_session_end. Shorter than VariableExtractor's own 8s per-call timeout
# on purpose: this runs after the caller has already gone.
_FINISH_WORKFLOW_TIMEOUT_S = 3.0

# Spoken when policies.max_call_duration_s is exceeded (see
# PipelineConversationHandler.on_speech_ended's check, right after STT).
# Fixed rather than drawn from the graph, same posture as
# _FALLBACK_LLM_ERROR below — deliberately not an end step's closing words,
# since those are written for a natural end-of-conversation goodbye and
# would misleadingly imply the conversation just happened to finish, not
# that a time limit cut it off.
_MAX_DURATION_GOODBYE = (
    "We're at the time limit for this call now. Thanks for calling — goodbye."
)

# Spoken when the LLM/tool-orchestrator stream raises (provider 5xx/429,
# network error, a bridging bug) partway through a turn. Without this the
# caller hears dead air for that whole turn — the exception was already
# swallowed here (see except block below) so the call itself survives, but
# silence reads as a dropped call to a real caller. Not scripted per-agent
# (like farewell_message) since this is a transport-failure fallback, not a
# conversational choice — same posture as _FALLBACK_GOODBYE above.
_FALLBACK_LLM_ERROR = "Sorry, I'm having a little trouble right now. Could you say that again?"

# Spoken the instant a tool call starts — covers dead air during a slow
# tool round-trip (e.g. a calendar API call). Rotates (not one fixed
# phrase) since a multi-tool-call turn repeating the same line sounded
# robotic; see _TOOL_CALL_FILLER_MIN_GAP_S for the other half (spacing).
_TOOL_CALL_FILLERS = (
    "Let me check that for you.",
    "One moment.",
    "Just a second.",
    "Give me a moment.",
)

# Collapses a rapid-fire tool-call burst (no real user speech between
# calls — see orchestrator.py's run_turn() while-loop) down to one filler
# instead of several stacked back to back.
_TOOL_CALL_FILLER_MIN_GAP_S = 4.0

# Spoken on the caller's very first utterance, before the LLM call starts —
# masks turn-1 latency (real LLM round-trip, not a special cold-start
# spike). Generic/fixed so it works regardless of what the caller said.
_FIRST_TURN_FILLER = "Mm-hmm, one moment."

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
# Fixed wording, for the same reason as _END_CALL_INSTRUCTION above: a
# transfer step in the graph is where "hand this call over" is configured,
# so this is only the anywhere-in-the-call escape hatch. The destination and
# type still come from config — those are operational, not conversational.
_TRANSFER_CONDITION = (
    "If the caller explicitly asks to speak to a human agent or "
    "representative"
)


def _build_transfer_instruction(transfer_type: str, destination: str) -> str:
    token = (
        f'[[TRANSFER type="{transfer_type}" destination="{destination}" '
        'reason="caller_requested_human"]]'
    )
    return (
        f"\n\n{_TRANSFER_CONDITION}, briefly acknowledge that you will connect "
        f"them, then end your reply with the exact token {token} on its own, "
        "after your spoken words. Only use this token when that condition is "
        "met — never say it out loud or explain it to the caller."
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
# or confuse a later turn. Deliberately not a change to the composed
# node prompt or
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


# Spoken instead of the agent's own transfer_announcement (if any) when a
# fabricated booking claim is what triggered this specific transfer.
# Product decision, confirmed live: without this, a caller who just heard
# "Confirmed! I'll book..." got silently handed off to a human moments
# later with zero explanation — which reads as the system being broken
# even though escalation is working exactly as designed. Framing this as
# a deliberate double-check, not a mystery hang-up, is the fix.
_BOOKING_FABRICATION_TRANSFER_ANNOUNCEMENT = (
    "Let me just double-check that booking with a team member to make sure "
    "it's set up correctly — one moment."
)


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
        transcripts:   TranscriptBuilder | None = None,
        tenant_id:     str = "",
        call_id:       str = "",
        direction:     str = "inbound",
        caller_number: str = "",
        called_number: str = "",
        knowledge:     IKnowledgeProvider | None = None,
        metrics:       IMetrics | None = None,
        tool_orchestrator: ToolCallOrchestrator | None = None,
        has_booking_tool: bool = False,
        use_workflow_draft: bool = False,
        text_only:     bool = False,
    ) -> None:
        self._stt          = provider_bundle.stt
        self._llm          = provider_bundle.llm
        self._tts          = provider_bundle.tts
        self._sample_rate  = sample_rate
        self._max_history  = max_history
        # Text-chat session (Admin UI): no STT ran and no TTS will run, so
        # every place the pipeline would have spoken yields the words
        # instead. See _speak() and the proto's text_only.
        self._text_only    = text_only
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
        # What the agent says at each moment of the call now lives entirely in
        # the graph — the always-on instruction on its global node, the
        # closing words on its end steps (docs/workflow.md §9.1). What is
        # left here is mechanics: date grounding, optional caller-number
        # booking context, and the two directive tokens, which are fixed so
        # no configuration can break the parser. Handed to WorkflowRunner,
        # which slots each node's own prompt in front of it — see
        # WorkflowRunner.system_prompt(). Unconditional now: there is no
        # agent without a graph, so there is no case where the end-call
        # token has no prompt to be appended to.
        self._has_booking_tool = has_booking_tool
        self._prompt_suffix = (
            _build_current_date_context()
            + (_build_caller_number_context(self._caller_number) if has_booking_tool else "")
            + _END_CALL_INSTRUCTION
        )
        self._goodbye_grace_period_ms = runtime_config.policies.goodbye_grace_ms
        # Admin-configured hard ceiling on call length (agents.max_call_
        # duration_s) — None means unlimited, the pre-existing behavior.
        # _call_started_at is this handler's own construction time, which
        # is effectively "call start" (PipelineConversationHandler is built
        # fresh per call — see servicer.py's handler_factory). Checked in
        # on_speech_ended(), not via a separate timer task: turn boundaries
        # already happen frequently enough in a real conversation (bounded
        # by the gateway's own no_speech_timeout/max_utterance_timeout) for
        # a per-turn check to catch the limit promptly, without adding a
        # second concurrent trigger path into the servicer's single-
        # generator Converse() loop.
        self._max_call_duration_s = runtime_config.policies.max_call_duration_s
        self._call_started_at = time.monotonic()
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
            else:
                # Auto-inject the transfer trigger instruction — same "append
                # to the prompt here, not conditionally later" treatment as
                # _END_CALL_INSTRUCTION above. Config validated above: a
                # transfer the LLM can request but nothing can complete would
                # strand callers mid-"connecting you now".
                self._prompt_suffix += _build_transfer_instruction(
                    tt, self._transfer_destination_default,
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
        # (pending_transfer / transfer_requested on _SessionState).
        self._transfer_engine = TransferDecisionEngine(self._metrics)
        self._guardrail_counter = GuardrailCounter()
        # Deliberately separate from _guardrail_counter above: that one is
        # reset every turn the caller's own utterance isn't flagged (see
        # on_speech_ended's STT-guardrail block) — sharing it with the
        # booking-fabrication check meant a polite caller's very next turn
        # (e.g. "Sure, thank you") erased the fabrication count before it
        # could ever exceed 1, so two consecutive fabricated "Booked!"
        # claims in the same real call (confirmed live) never
        # escalated. This counter only ever moves in response to the AI's
        # own fabrication, never the caller's tone.
        self._booking_fabrication_counter = GuardrailCounter()
        # Everything else this handler tracks per live session_id — see
        # _SessionState's own docstring for why these were consolidated
        # (history, cancellation event, pending transfer, fabrication/
        # phone-confirmation/filler state, etc.) instead of a dozen
        # separate top-level dicts/sets. Lazily created per session by
        # _session() below; on_session_end() drops the whole entry in one
        # line instead of one pop/discard per field.
        self._sessions: dict[str, _SessionState] = {}
        # Conversation workflow (docs/workflow.md). Always present — an agent
        # IS its workflow (§9.1), graph_for() falls back to the starter graph
        # rather than returning None, and there is no single-prompt mode left
        # to branch on.
        self._last_reported_node_id: str | None = None
        now = datetime.now(timezone.utc)
        self._extractor = VariableExtractor(self._llm, self._on_variables_extracted)
        # Threshold derived from this pipeline's own trim cap rather than
        # taken as a default — the two are the same constraint, and chosen
        # separately they drifted far enough apart to disable
        # summarization entirely (see summary_threshold_for).
        self._summarizer = ContextSummarizer(
            self._llm, threshold_msgs=summary_threshold_for(max_history),
        )
        graph = graph_for(runtime_config, draft=use_workflow_draft)
        self._workflow = WorkflowRunner(
            graph,
            # The always-on instruction comes from the graph's own global
            # node — see WorkflowRunner.__init__. Nothing beside the graph
            # contributes conversation text any more.
            base_suffix=self._prompt_suffix,
            variables={
                "caller_number": caller_number,
                "called_number": called_number,
                "direction":     direction,
                "agent_name":    runtime_config.agent.name,
                "business_name": runtime_config.tenant.name,
                "current_date":  now.strftime("%Y-%m-%d"),
                "current_time":  now.strftime("%H:%M"),
                # ponytail: per-contact campaign fields (docs/workflow.md
                # §5.7's second source) would slot in here — they have
                # nowhere to come from today: campaign_contacts stores
                # only phone_number/name, and no channel-variable path
                # carries either to this process. Wire it when contacts
                # grow custom fields; this dict is the only seam it needs.
            },
            extractor=self._extractor,
            summarizer=self._summarizer,
        )
        if (
            any(n.type == "transfer" for n in graph.nodes.values())
            and self._transfer_type_default in ("", "none")
        ):
            # Same fail-loud-at-setup posture as the transfer-destination
            # validation above: a transfer node the engine will always
            # reject is a caller stranded mid-"connecting you now".
            log.error(
                "Workflow for agent %s has a transfer node but the agent's "
                "transfer_type is 'none' — those transfers will be rejected; "
                "set warm/cold on the agent's Escalation config",
                runtime_config.agent.slug,
            )
        log.info(
            "Workflow active for agent %s: %d nodes, starting at %r",
            runtime_config.agent.slug, len(graph.nodes), graph.start.name,
        )

    def _on_variables_extracted(self, values: dict) -> None:
        """Extraction results merge into the runner, so later nodes' prompts
        can reference them, and get persisted to calls.extracted_variables
        at session end."""
        self._workflow.update_variables(values)

    # ── IConversationHandler ───────────────────────────────────────────────────

    async def greeting(self, session_id: str) -> list[bytes]:
        if self._transcripts is not None:
            self._transcripts.begin_call(
                session_id, self._tenant_id, self._call_id,
                self._direction, self._caller_number, self._called_number,
                self._agent_id, self._agent_config_version,
            )
        # Speaking the instant the line opens gets the first syllable
        # clipped on some outbound carriers — the start node can hold off.
        # 0 (the default) is the behavior every call has today.
        if self._workflow.delayed_start_ms > 0:
            await asyncio.sleep(self._workflow.delayed_start_ms / 1000)
        text = self._workflow.greeting() or ""
        if not text:
            return []
        return [chunk async for chunk in self._synthesize_sentence_stream(text, session_id)]

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
        self._session(session_id).cancelled = cancel_event

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

        async for response in self._run_turn(
            session_id, stt_result.text, stt_result.confidence, cancel_event, turn_start, stt_ms,
        ):
            yield response

    async def on_text(
        self, session_id: str, text: str,
    ) -> AsyncGenerator[HandlerResponse, None]:
        """A typed caller turn (text_only sessions — see the proto's
        TextInput). Everything downstream of STT is the same code the voice
        path runs, so a workflow tested in the chat panel is the workflow
        that will run on a call: same prompts, same transitions, same tool
        narrowing, same extraction, same end/transfer handling."""
        text = text.strip()
        if not text:
            return
        # Fresh per-turn cancel event, same contract as on_speech_ended's.
        cancel_event = asyncio.Event()
        self._session(session_id).cancelled = cancel_event
        yield HandlerResponse(stt_text=text, stt_confidence=1.0)
        async for response in self._run_turn(
            session_id, text, 1.0, cancel_event, time.monotonic(), None,
        ):
            yield response

    async def _run_turn(
        self,
        session_id:   str,
        user_text:    str,
        confidence:   float,
        cancel_event: asyncio.Event,
        turn_start:   float,
        stt_ms:       float | None,
    ) -> AsyncGenerator[HandlerResponse, None]:
        """One conversation turn, from the caller's words to the agent's
        reply. Split out of on_speech_ended so on_text can reach it without
        a microphone; `stt_ms` is None for a typed turn, where there was no
        recognition step to time."""
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
        violation = GuardrailDetector.check(user_text)
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

        # ── Max call duration ────────────────────────────────────────────────
        # Checked here — after STT, so the caller's final utterance is still
        # transcribed/recorded, but before the LLM call, so we never pay for
        # (and then discard) a generated response. Skips the LLM entirely and
        # speaks a fixed, deterministic wrap-up line — same "scripted, not
        # LLM-judged" posture as _FALLBACK_GOODBYE, and reliable
        # regardless of what the model would have said. See __init__ for why
        # this is a per-turn check rather than a separate timer task.
        if (
            self._max_call_duration_s is not None
            and (time.monotonic() - self._call_started_at) >= self._max_call_duration_s
        ):
            log.info(
                "Max call duration (%ds) reached — ending call session=%s",
                self._max_call_duration_s, session_id,
            )
            got_audio = False
            async for response in self._speak(_MAX_DURATION_GOODBYE, session_id):
                got_audio = True
                yield response
            if not got_audio:
                # Synthesis failed outright — fall back so tts_started_sent
                # still flips true on the gateway side (see servicer.py);
                # otherwise EndCall below would be silently dropped, same
                # reasoning as _FALLBACK_GOODBYE.
                async for response in self._speak(_FALLBACK_GOODBYE, session_id):
                    yield response
            if self._transcripts is not None:
                self._transcripts.record_turn(
                    session_id, user_text, confidence,
                    _MAX_DURATION_GOODBYE, False,
                    latency=TurnLatency(
                        stt_ms=stt_ms,
                        stt_engine=None if self._text_only else type(self._stt).__name__,
                        tts_engine=None if self._text_only else type(self._tts).__name__,
                    ),
                )
            yield HandlerResponse(
                end_call=True,
                end_call_grace_period_ms=self._goodbye_grace_period_ms,
            )
            return

        # ── 2. LLM ─────────────────────────────────────────────────────────────
        history = self._get_history(session_id)
        # Prepend per-agent system prompt as the first message if configured.
        # history[0] is always the active node's composed prompt —
        # _refresh_node_prompt inserts it when the history is empty and
        # replaces it every turn after that.
        is_first_turn = not history
        self._refresh_node_prompt(history)
        history.append(ChatMessage(role="user", content=user_text))

        # See _FIRST_TURN_FILLER's own comment — masks the caller's first
        # wait (knowledge retrieval + LLM + TTS, all still to come below)
        # instead of leaving them in silence for it. Spoken here, before
        # any of that work starts, not overlapped with it — same posture
        # as the tool-call filler elsewhere in this method.
        if is_first_turn and not self._session(session_id).first_turn_filler_spoken:
            self._session(session_id).first_turn_filler_spoken = True
            async for response in self._speak(_FIRST_TURN_FILLER, session_id):
                yield response

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
        # In workflow mode retrieval is per-stage: a node with no knowledge
        # base attached does no retrieval at all, the same restrictive
        # reading as its tool list. Unchanged for every other agent.
        if self._knowledge is not None and self._workflow.knowledge_enabled():
            context = await self._retrieve_context(user_text, session_id)
            if context is not None and context.chunks:
                augmented = ChatMessage(
                    role="user",
                    content=f"{self._format_context(context)}\n\nCaller's question: {user_text}",
                )
                messages_for_llm = history[:-1] + [augmented]

        directives: list[Directive] = []
        full_response: list[str] = []
        tool_calls_made: list[str] = []
        end_call = False
        any_audio = False
        llm_t0 = time.monotonic()
        first_token_at: float | None = None
        first_audio_at: float | None = None
        try:
            async for chunk, tts_audio, marker_seen in self._llm_to_tts(
                messages_for_llm, cancel_event, session_id, directives,
                tool_calls_made, store=history,
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

        # Confirmed live: a local Ollama model (qwen2.5:7b)
        # narrated "Booked! You have a demo scheduled..." with zero real
        # book_appointment call behind it — Cal.com had no such booking.
        # generate_with_tools() only ever detects a tool call when Ollama's
        # response actually populates message.tool_calls; a model that just
        # writes prose instead is never flagged anywhere upstream. Can't
        # unspeak a sentence already streamed to TTS (see _llm_to_tts's
        # per-sentence-boundary synthesis), so this is damage control, not
        # prevention: nudge the *next* turn to actually call the tool, and
        # count it as a guardrail violation so repeat offenses escalate to
        # a human via the existing TransferDecisionEngine path.
        # Confirmed live, separately: a genuine booking success on an
        # earlier turn, truthfully recapped by the LLM on a LATER turn
        # (no tool call needed that turn — nothing about the booking
        # changed), got flagged as a fresh fabrication anyway, since this
        # check only ever looks at THIS turn's tool_calls_made. Comparing
        # against the real confirmed slot (see confirmed_booking_slot,
        # set in _token_stream's DeterministicSpokenEvent handling) lets a
        # truthful recap of THAT exact slot through without disabling the
        # check for the rest of the call — confirmed live, separately
        # again: a caller who later asked to reschedule to a genuinely
        # different, never-confirmed time got a false claim about THAT
        # new time waved through too, when this was tracked as a one-way
        # "a booking happened at some point" flag instead.
        _confirmed_slot = self._session(session_id).confirmed_booking_slot
        recap_of_real_booking = (
            _confirmed_slot is not None
            and _claim_matches_confirmed_slot(assistant_text, _confirmed_slot)
        )
        fabricated_booking_claim = (
            self._has_booking_tool
            and not recap_of_real_booking
            and "book_appointment" not in tool_calls_made
            and _claims_booking_without_tool_call(assistant_text)
        )

        if full_response and not cancel_event.is_set():
            # A cancelled response (≥1 token, interrupted) is treated as zero
            # tokens: discard it so history only ever holds complete pairs.
            # Marker stripped here too — full_response is raw per-token text, so
            # a marker split across tokens is only contiguous once joined, and
            # it must never leak into history or get referenced in a later turn.
            history.append(ChatMessage(role="assistant", content=assistant_text))
            if fabricated_booking_claim:
                log.warning(
                    "Possible fabricated booking claim (no book_appointment call this turn) "
                    "session=%s text=%r", session_id, assistant_text,
                )
                history.append(ChatMessage(
                    role="system",
                    content=(
                        "Correction: nothing was actually booked, confirmed, or scheduled just "
                        "now — you did not call book_appointment. If the caller still wants an "
                        "appointment, call book_appointment for real before saying anything is "
                        "booked or confirmed."
                    ),
                ))
                self.record_booking_fabrication(session_id)
            self._trim_history(session_id)
        else:
            # Barge-in, cancel, or LLM failure: remove the unpaired user message
            # so history stays consistent for the next turn's LLM context.
            if history and history[-1].role == "user":
                history.pop()

        if self._transcripts is not None:
            self._transcripts.record_turn(
                session_id, user_text, confidence,
                assistant_text, cancel_event.is_set(),
                latency=TurnLatency(
                    stt_ms=stt_ms, llm_ms=llm_ms, tts_ms=tts_ms, voice_to_voice_ms=voice_to_voice_ms,
                    # Naming an engine that never ran is worse than naming
                    # none: a typed turn transcribed nothing and spoke
                    # nothing, and call analytics shouldn't read as if it did.
                    stt_engine=None if self._text_only else type(self._stt).__name__,
                    llm_engine=type(self._llm).__name__,
                    tts_engine=None if self._text_only else type(self._tts).__name__,
                ),
                # The node as of the END of the turn: if the model
                # transitioned mid-turn, it generated this reply under the
                # new node's prompt, so that is the stage that said it.
                node_id=self._workflow.node.id,
                node_name=self._workflow.node.name,
            )

        # In a chat session nothing above produced audio, so the reply the
        # model actually generated is delivered here instead. One message
        # per turn, after the tool calls and any mid-turn transition have
        # settled, so the text matches what a caller would have heard.
        if self._text_only and assistant_text and not cancel_event.is_set():
            any_audio = True
            yield HandlerResponse(agent_text=assistant_text)

        # Report the transition (if any) before the turn's terminal events —
        # the editor's canvas should light up the node that just spoke, even
        # on a turn that also ends the call.
        node_changed = self._take_node_changed()
        if node_changed is not None:
            yield HandlerResponse(node_changed=node_changed)

        # Reaching an `end` node ends the call the same way the [[END_CALL]]
        # token does — one teardown path, not two.
        ended_on_end_node = self._workflow.pending_end
        if ended_on_end_node:
            end_call = True
        elif end_call:
            # The model emitted [[END_CALL]] from a node that is not an end
            # node. That instruction is appended to every node's prompt on
            # purpose — it is the safety net for a caller who says goodbye
            # where no edge covers it, and without it the call would hang on
            # until max_call_duration_s. But it skips the end node that would
            # have carried the disposition, so the runner records that the
            # call left the graph instead of reporting no outcome at all.
            self._workflow.ended_off_graph = True

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
        # reasoning as end_call below.
        #
        # Computed BEFORE the end_call block below (confirmed live):
        # a fabricated booking claim that also happened to
        # trip the LLM's own [[END_CALL]] marker in the same turn — a
        # natural "wrap up and say goodbye" response shape — got its
        # transfer silently dropped every time, because end_call used to
        # be handled unconditionally first and yield HandlerResponse(
        # end_call=True) before this code ever ran; the servicer tears the
        # session down on that signal, so the transfer_request yielded
        # afterward in the same generator never had anywhere to land.
        # Handing off to a human is a strictly bigger deal than the agent's
        # own decision to hang up, so a pending transfer must win outright.
        transfer_request: TransferRequest | None = None
        if not cancel_event.is_set():
            transfer_directive = next(
                (d for d in directives if isinstance(d, TransferDirective)), None,
            )
            if transfer_directive is not None:
                decision = self._transfer_engine.evaluate(
                    self._decision_context(session_id),
                    TransferTrigger(type=TriggerType.LLM_DIRECTIVE, directive=transfer_directive),
                )
                if decision.accepted:
                    transfer_request = decision.request
                    self._session(session_id).transfer_requested = True
            # Reaching a `transfer` node hands the call to the same transfer
            # engine an LLM directive would — modelling handoff as a tool
            # instead would route around transfer_engine, the caller-ID
            # policy, and the gateway's own Transferring state.
            if transfer_request is None:
                transfer_request = await self._workflow_transfer(session_id)

            # Fall through to a pending escalation-accepted request whenever
            # the directive path produced nothing — including when a
            # directive WAS emitted but the engine rejected it as
            # already_transferring precisely BECAUSE that pending request
            # exists. Without this, the accepted pending transfer would be
            # starved for as long as the LLM keeps re-emitting directives.
            if transfer_request is None:
                state = self._session(session_id)
                transfer_request = state.pending_transfer
                state.pending_transfer = None

        # Signal the servicer to end the call once this turn's audio has
        # streamed — but not if the caller barged in (cancel_event.is_set()):
        # them talking means the agent's decision to end the call is stale.
        # Also not if a transfer is about to happen instead (see comment
        # above) — ending the call would make the transfer unreachable.
        if end_call and not cancel_event.is_set() and transfer_request is None:
            # The closing words are the end step's own — its prompt drove
            # this turn's generation (the transition swaps history[0]
            # mid-turn), so by here the goodbye is already streaming. There
            # is no second, agent-level farewell to speak on top of it; that
            # column existed to override the graph and is gone.
            if not any_audio:
                # Marker-only reply with nothing spoken (the model emitted
                # [[END_CALL]] and no words): synthesize a fallback so the
                # servicer actually has audio to key EndCall off of.
                async for response in self._speak(_FALLBACK_GOODBYE, session_id):
                    yield response
            yield HandlerResponse(
                end_call=True,
                end_call_grace_period_ms=self._goodbye_grace_period_ms,
            )

        if transfer_request is not None:
            # A fabrication-triggered transfer always gets its own specific
            # line — see that constant's comment for why a silent/generic
            # handoff right after a false "booked" claim reads as the
            # system being broken. Workflow transfer nodes / LLM-directive
            # transfers already spoke their acknowledgment via the node's
            # own prompt or the model's reply.
            if self._session(session_id).fabrication_triggered_transfer:
                self._session(session_id).fabrication_triggered_transfer = False
                async for response in self._speak(
                    _BOOKING_FABRICATION_TRANSFER_ANNOUNCEMENT, session_id,
                ):
                    yield response
            yield HandlerResponse(transfer_request=transfer_request)

    async def on_cancel(self, session_id: str) -> None:
        self._cancel_event(session_id).set()

    async def on_session_end(self, session_id: str, reason: str,
                             final_state: str | None = None) -> None:
        # Before the transcript writes below — record_workflow_outcome has
        # to land on the calls row while TranscriptBuilder's per-session
        # write chain is still alive (end_call() drops it).
        #
        # Bounded: ConversationSession.close() and the servicer both await
        # this with no timeout of their own, so an unbounded extraction
        # round-trip here would hold the gRPC stream (and the calls row's
        # finalization) open for its full timeout after the caller has
        # already hung up. A missed final extraction is analytics; a call
        # that takes 8 seconds to disappear from the live list is not.
        try:
            await asyncio.wait_for(
                self._finish_workflow(session_id), timeout=_FINISH_WORKFLOW_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.warning(
                "Workflow finalization timed out after %.1fs session=%s — "
                "the call's path and disposition may be incomplete",
                _FINISH_WORKFLOW_TIMEOUT_S, session_id,
            )
        # Requirement: transfer-failure recovery turns are recorded in
        # short-term memory (history) immediately, in on_transfer_failed(),
        # but their transcript *persistence* is deferred until now — normal
        # SessionEnd — rather than written immediately like every other
        # turn's record_turn() call.
        state = self._sessions.get(session_id)
        pending_recovery_turns = state.pending_recovery_turns if state is not None else []
        if self._transcripts is not None:
            for caller_text, ai_response, interrupted in pending_recovery_turns:
                self._transcripts.record_turn(session_id, caller_text, 1.0, ai_response, interrupted)
            self._transcripts.end_call(session_id, reason, final_state=final_state)
        self._guardrail_counter.reset(session_id)
        self._booking_fabrication_counter.reset(session_id)
        self._sessions.pop(session_id, None)
        try:
            await self._stt.cancel_stream(session_id)
        except Exception:
            log.exception("STT cancel_stream failed session=%s", session_id)

    def _decision_context(
        self, session_id: str, destination_override: str | None = None,
    ) -> DecisionContext:
        """destination_override is a workflow transfer node's own
        destination (see docs/workflow.md §2.3) — it replaces the agent-wide
        default for that one decision and nothing else."""
        return DecisionContext(
            session_id=session_id, tenant_id=self._tenant_id, call_id=self._call_id,
            transfer_type=self._transfer_type_default,
            transfer_destination=destination_override or self._transfer_destination_default,
            escalation_threshold=self._escalation_threshold,
            already_requested=self._session(session_id).transfer_requested,
            caller_id_policy=self._caller_id_policy,
            platform_did=self._platform_did,
            custom_caller_id=self._custom_caller_id,
            caller_number=self._caller_number,
            waiting_experience=self._waiting_experience,
        )

    # ── Workflow helpers ─────────────────────────────────────────────────

    def _take_node_changed(self) -> NodeChanged | None:
        """Whether the active node moved since the last time this was
        asked. Diffed rather than pushed from the runner, so the runner
        keeps knowing nothing about gRPC messages or who is watching."""
        node = self._workflow.node
        if node.id == self._last_reported_node_id:
            return None
        self._last_reported_node_id = node.id
        return NodeChanged(
            node_id=node.id, node_name=node.name, node_type=node.type,
            via=self._workflow.last_transition,
        )

    def _refresh_node_prompt(self, history: list[ChatMessage]) -> None:
        """history[0] is the active node's prompt, refreshed every turn —
        the node may have changed last turn, and its prompt is re-rendered
        with whatever variables have been extracted since. _trim_history
        already preserves history[0], so this is a one-slot mutation, not a
        context rework."""
        prompt = ChatMessage(role="system", content=self._workflow.system_prompt())
        if history and history[0].role == "system":
            history[0] = prompt
        else:
            history.insert(0, prompt)

    async def _workflow_transfer(self, session_id: str) -> TransferRequest | None:
        node = self._workflow.pending_transfer
        if node is None:
            return None
        self._workflow.pending_transfer = None
        # A destination that reads {{ some_variable }} cannot be resolved
        # against a value still in flight — flush first (see
        # VariableExtractor.flush).
        await self._extractor.flush()
        destination = self._workflow.render(node.transfer_destination or "") or None
        decision = self._transfer_engine.evaluate(
            self._decision_context(session_id, destination_override=destination),
            TransferTrigger(
                type=TriggerType.WORKFLOW, workflow_reason=f"workflow_node:{node.name}",
            ),
        )
        if not decision.accepted:
            log.warning(
                "Workflow transfer node %r rejected: %s session=%s",
                node.name, decision.rejection_reason, session_id,
            )
            return None
        self._session(session_id).transfer_requested = True
        return decision.request

    async def _finish_workflow(self, session_id: str) -> None:
        """One final extraction pass on whatever node the call ended in,
        then the call's path and outcome. Idempotent by way of
        VariableExtractor's own guard — several teardown paths converge
        here (caller hangs up, agent ends, max duration, transfer
        completes) and without that they race to write the same row."""
        self._summarizer.cancel()
        try:
            await self._extractor.extract_final(
                self._workflow.node, self._get_history(session_id),
            )
            await self._extractor.flush()
        except Exception:
            log.exception("Workflow final extraction failed session=%s", session_id)
        if self._transcripts is not None:
            self._transcripts.record_workflow_outcome(
                session_id,
                nodes_visited=self._workflow.visited,
                disposition=self._workflow.disposition,
                extracted_variables=self._workflow.variables,
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
        return self._evaluate_escalation(session_id, self._guardrail_counter.increment(session_id))

    def record_booking_fabrication(self, session_id: str) -> TransferRequest | None:
        """Same escalation mechanics as record_guardrail_violation(), but
        counted on _booking_fabrication_counter — see that field's own
        comment for why this can't share the caller-frustration counter."""
        request = self._evaluate_escalation(session_id, self._booking_fabrication_counter.increment(session_id))
        if request is not None:
            self._session(session_id).fabrication_triggered_transfer = True
        return request

    def _evaluate_escalation(self, session_id: str, count: int) -> TransferRequest | None:
        decision = self._transfer_engine.evaluate(
            self._decision_context(session_id),
            TransferTrigger(type=TriggerType.ESCALATION, violation_count=count),
        )
        if not decision.accepted:
            return None
        state = self._session(session_id)
        state.transfer_requested = True
        state.pending_transfer = decision.request
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
        self._session(session_id).cancelled = cancel_event

        # This attempt is over (unsuccessfully) — the call continues, so a
        # caller who asks again must get a fresh TransferDecisionEngine
        # evaluation, not an "already_transferring" rejection.
        self._session(session_id).transfer_requested = False
        # The speculative summary started on TransferInitiated (see
        # start_finalization()) was for a call that was about to end — it
        # isn't, so throw it away rather than let it run unattended.
        self._session_finalizer.discard_pending_summary(session_id)

        history = self._get_history(session_id)
        # Same one-slot refresh as a normal turn: a recovery turn must run
        # under the node the call is actually in, not whatever history[0]
        # happened to hold.
        self._refresh_node_prompt(history)

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
                messages_for_llm, cancel_event, session_id, directives, store=history
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
            async for response in self._speak(assistant_text, session_id):
                yield response

        # Recorded as a normal assistant turn (memory) — no matching
        # "user" turn is stored, same asymmetry RAG-augmented turns already
        # tolerate; the ephemeral notice above is never persisted.
        if assistant_text and not cancel_event.is_set():
            history.append(ChatMessage(role="assistant", content=assistant_text))
            self._trim_history(session_id)

        # Requirement: do not persist memory immediately — buffer this turn
        # and only write it during normal SessionEnd (see on_session_end()).
        # Short-term memory (session history above) already happened; this is
        # about deferring the transcript *persistence* specifically.
        self._session(session_id).pending_recovery_turns.append((
            f"[transfer_failed: {reason}]", assistant_text, cancel_event.is_set(),
        ))

    def on_transfer_cancelled(self, session_id: str) -> None:
        """A pending transfer was dropped before dispatch (caller barge-in
        during the acknowledgment — see ConversationSession.
        on_transfer_cancelled). Release the duplicate-suppression flag: the
        gateway never received this attempt, so a caller who barges in and
        then asks again must get a fresh TransferDecisionEngine evaluation,
        not an "already_transferring" rejection."""
        self._session(session_id).transfer_requested = False
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
        self,
        history: list[ChatMessage],
        session_id: str,
        cancel_event: asyncio.Event,
        tool_calls_made: list[str],
        store: list[ChatMessage] | None = None,
    ) -> AsyncGenerator[str | ToolCallStartedEvent | LocalToolCompletedEvent, None]:
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

        # Force book_appointment specifically on the one turn where it's
        # unambiguous the LLM should call it right now — see
        # _caller_just_confirmed_phone_number's own docstring. has_booking_tool
        # already gates whether this agent has the tool at all (same flag
        # _build_caller_number_context checks) — no point forcing a tool
        # this agent was never given.
        just_confirmed = self._has_booking_tool and _caller_just_confirmed_phone_number(
            history, self._caller_number,
        )
        force_tool_name = "book_appointment" if just_confirmed else None
        if just_confirmed:
            self._session(session_id).phone_number_confirmed = True

        # A workflow turn additionally offers one in-process tool per
        # outgoing edge of the active node (the transitions), and narrows
        # the agent's DB-backed tools to the ones this node allows.
        # Passed as callables, not values: a transition changes the active
        # node mid-turn, and the orchestrator re-reads these after every
        # local tool call so the rest of the turn gets the new node's tools
        # rather than the ones it just left.
        local_tools = lambda: self._workflow.local_tools(history, store)  # noqa: E731
        only_tools = lambda: self._workflow.allowed_tool_names()       # noqa: E731

        async for event in self._tool_orchestrator.run_turn(
            self._agent_id or "", self._tenant_id, self._call_id, session_id, history,
            caller_number=self._caller_number, cancel_event=cancel_event,
            force_tool_name=force_tool_name,
            phone_number_confirmed=self._session(session_id).phone_number_confirmed,
            local_tools=local_tools, only_tools=only_tools,
        ):
            if isinstance(event, ToolCallStartedEvent):
                tool_calls_made.append(event.tool_name)
                yield event
                continue
            if isinstance(event, LocalToolCompletedEvent):
                yield event
                continue
            if isinstance(event, DeterministicSpokenEvent):
                # Unwrapped to plain text like a TokenEvent (same sentence-
                # splitting/TTS path) rather than given its own handling —
                # tool_calls_made already contains this turn's tool name by
                # now (see ToolCallStartedEvent above), so the fabrication
                # check downstream is already correctly inert for THIS
                # turn's text; no special-casing needed beyond getting it
                # spoken. But record the real confirmed slot persistently
                # too — see confirmed_booking_slot's own comment for why
                # a later turn's truthful recap needs this to avoid a false
                # fabrication flag, and why it must be the actual datetime,
                # not just a boolean "a booking happened at some point."
                if event.confirmed_datetime:
                    self._session(session_id).confirmed_booking_slot = event.confirmed_datetime
                yield event.text
                continue
            assert isinstance(event, ToolTokenEvent)
            yield event.text

    async def _llm_to_tts(
        self,
        history:      list[ChatMessage],
        cancel_event: asyncio.Event,
        session_id:   str,
        directives:   list[Directive],
        tool_calls_made: list[str] | None = None,
        store:        list[ChatMessage] | None = None,
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
        if tool_calls_made is None:
            tool_calls_made = []
        # Initialized before the loop, and carried by every yield below. The
        # caller does `end_call = marker_seen` on EVERY item it receives (see
        # on_speech_ended), so a branch yielding a literal False after the
        # marker was already seen silently un-hangs-up the call — and the
        # branches that did so (filler, transition speech) are exactly the
        # ones that can fire in the same turn as an [[END_CALL]].
        end_call = False
        try:
            async for item in self._token_stream(
                history, session_id, cancel_event, tool_calls_made, store,
            ):
                if cancel_event.is_set():
                    break
                if isinstance(item, LocalToolCompletedEvent):
                    # A workflow transition just happened. Speak its
                    # bridging line now, during the model's round-trip to
                    # the new node's prompt, instead of leaving dead air —
                    # and barge-in-able like any other speech, since the
                    # loop above checks cancel_event on every item.
                    speech = self._workflow.pending_speech
                    if speech:
                        self._workflow.pending_speech = None
                        if self._text_only:
                            # _synthesize_sentence_stream is a no-op in a chat
                            # session, so the words have to be yielded as text
                            # or the bridging line vanishes entirely. Every
                            # other scripted line goes through _speak() for
                            # exactly this reason, and a workflow tested in the
                            # chat panel has to show what a call would say.
                            yield speech, b"", end_call
                        else:
                            async for chunk in self._synthesize_sentence_stream(speech, session_id):
                                yield "", chunk, end_call
                    continue
                if isinstance(item, ToolCallStartedEvent):
                    # Rotates through _TOOL_CALL_FILLERS, gap-suppressed by
                    # _TOOL_CALL_FILLER_MIN_GAP_S — see both constants' own
                    # comments for why neither alone was enough.
                    now = time.monotonic()
                    state = self._session(session_id)
                    last_spoken = state.tool_call_filler_last_spoken
                    if last_spoken is None or (now - last_spoken) >= _TOOL_CALL_FILLER_MIN_GAP_S:
                        state.tool_call_filler_last_spoken = now
                        idx = state.tool_call_filler_index
                        state.tool_call_filler_index = idx + 1
                        phrase = _TOOL_CALL_FILLERS[idx % len(_TOOL_CALL_FILLERS)]
                        any_filler_chunk = False
                        async for chunk in self._synthesize_sentence_stream(phrase, session_id):
                            if not any_filler_chunk:
                                any_filler_chunk = True
                                log.info(
                                    "Tool-call filler spoken tool=%s phrase=%r session=%s",
                                    item.tool_name, phrase, session_id,
                                )
                            yield "", chunk, end_call
                        if self._text_only and not any_filler_chunk:
                            # text_only: synthesis is a no-op — still surface
                            # the filler as agent_text so chat sessions see it.
                            yield phrase, b"", end_call
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
                # The text first and unconditionally: synthesis yields
                # nothing in a text_only session (and can fail outright in a
                # voice one), and the caller keys the whole turn off
                # full_response — so gating the words on the audio would
                # lose the apology entirely.
                yield _FALLBACK_LLM_ERROR, b"", False
                async for chunk in self._synthesize_sentence_stream(_FALLBACK_LLM_ERROR, session_id):
                    yield "", chunk, False

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
        # The one shared boundary every text source reaches TTS through —
        # the LLM-token path already ran DirectiveParser.parse() upstream,
        # but the greeting and the fixed fallback/filler strings never do
        # (see strip_markdown_chars' docstring), so this
        # is where all of them get covered instead of at every call site.
        if self._text_only:
            # A chat session must not touch the TTS provider at all —
            # loading a voice model takes seconds and produces audio nobody
            # will play. Callers pair every synthesis site with _speak(),
            # which emits the words themselves in this mode.
            return
        text = strip_markdown_chars(text)
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

    async def _speak(self, text: str, session_id: str) -> AsyncGenerator[HandlerResponse, None]:
        """Deliver one fixed line to the caller — as synthesized audio on a
        call, as text in a chat session. Every scripted line (greeting,
        farewell, max-duration wrap-up, transfer announcement) goes through
        here so none of them can be silently dropped in text mode."""
        if self._text_only:
            if text.strip():
                yield HandlerResponse(agent_text=text)
            return
        async for chunk in self._synthesize_sentence_stream(text, session_id):
            yield HandlerResponse(tts_payloads=[chunk])

    def greeting_message(self) -> str:
        """The opening line as text, for a text_only session. Pure — the
        side effects (begin_call, the start node's delayed start) belong to
        greeting(), which runs first either way."""
        return self._workflow.greeting() or ""

    def _session(self, session_id: str) -> _SessionState:
        state = self._sessions.get(session_id)
        if state is None:
            state = _SessionState()
            self._sessions[session_id] = state
        return state

    def _cancel_event(self, session_id: str) -> asyncio.Event:
        return self._session(session_id).cancelled

    def _get_history(self, session_id: str) -> list[ChatMessage]:
        return self._session(session_id).history

    def _trim_history(self, session_id: str) -> None:
        history = self._session(session_id).history
        # Preserve a leading system message so it is not silently sliced away
        # when the conversation grows past max_history turns.  Without this,
        # history[-N:] drops history[0] and the `if not history` guard on the
        # next turn is never True, so the system prompt is never re-injected.
        base = 1 if (history and history[0].role == "system") else 0
        max_msgs = self._max_history * 2 + base
        if len(history) > max_msgs:
            # Spliced in place rather than rebound to a new list: a workflow
            # call's background summarization holds a reference to this exact
            # list and applies its result to it later (see
            # ContextSummarizer._summarize). Rebinding would leave that
            # summary mutating an orphan nobody reads — the work silently
            # thrown away. Same content either way.
            history[base:] = history[-self._max_history * 2:]
