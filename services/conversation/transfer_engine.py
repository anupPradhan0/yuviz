"""
TransferDecisionEngine — the single arbiter of *when* an AI conversation
transfers to a human (Phase 6 of AI-to-human transfer).

Before this module, two triggers each independently built a TransferRequest
and applied their own ad-hoc eligibility checks: the LLM-directive branch
in PipelineConversationHandler.on_speech_ended(), and
record_guardrail_violation()'s escalation-threshold check. Both are now
callers of this engine instead — it owns transfer eligibility, precedence
between multiple *concurrent* checks (destination configured, transfer
enabled, not a duplicate), reason typing, and TransferRequest construction.
It does not own dispatch: servicer.py still sends the resulting
TransferRequest to the gateway, and pipeline.py still owns the streaming
handshake (yielding HandlerResponse, holding a pending escalation request
for the next turn) — see project architecture review, "the engine owns the
decision; the pipeline owns the streaming lifecycle."

Design (per architect review):
  - TransferDecisionEngine.evaluate() is a pure, stateless function: same
    (context, trigger) in, same Decision out. No session map, no locks, no
    per-session lifecycle inside the engine — every fact it needs is
    supplied by the caller via DecisionContext each call. This keeps
    ConversationFSM as the *only* authority on session/transfer state
    (Idle/Listening/.../Transferring/Closing) — the engine never invents a
    second one that could drift from it.
  - TransferTrigger is a uniform envelope so new trigger producers (CRM
    escalation, sentiment, fraud, a future REST endpoint, a supervisor
    override) never require a new evaluate() signature — only a new
    TriggerType + payload field here, and a new caller building one.
  - Guardrail *counting* is deliberately not owned by the engine either
    (see guardrails.GuardrailCounter): the engine answers "given this
    violation count, should we transfer?", never "was this a violation?"
    or "what's the current count?".
  - TransferReason is an internal, strongly-typed vocabulary for
    observability. It maps onto the exact same free-text trigger/reason
    strings the event bus and gRPC wire already carry (see
    directives.TransferRequest.trigger, conversation.proto's
    TransferRequest.reason) — no protocol change, ever, from adding a new
    TransferReason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from .directives import TransferDirective, TransferRequest, TransferType, coerce_transfer_type
from .metrics import IMetrics, NullMetrics

log = logging.getLogger(__name__)


class TransferReason(str, Enum):
    """Internal reason vocabulary — observability only. .wire_trigger is
    the exact string written into TransferRequest.trigger (event bus) —
    CUSTOMER_REQUEST/ESCALATION_THRESHOLD map onto the two values already
    in use before this engine existed, so existing log greps/tests/
    dashboards keyed on "llm_directive"/"escalation_threshold" keep
    working unchanged."""
    CUSTOMER_REQUEST     = "CUSTOMER_REQUEST"
    ESCALATION_THRESHOLD = "ESCALATION_THRESHOLD"
    WORKFLOW_POLICY      = "WORKFLOW_POLICY"
    SYSTEM_REQUEST       = "SYSTEM_REQUEST"

    @property
    def wire_trigger(self) -> str:
        return _WIRE_TRIGGER[self]


_WIRE_TRIGGER: dict["TransferReason", str] = {
    TransferReason.CUSTOMER_REQUEST:     "llm_directive",
    TransferReason.ESCALATION_THRESHOLD: "escalation_threshold",
    TransferReason.WORKFLOW_POLICY:      "workflow_policy",
    TransferReason.SYSTEM_REQUEST:       "system_request",
}


class TriggerType(str, Enum):
    LLM_DIRECTIVE = "llm_directive"
    ESCALATION    = "escalation"
    WORKFLOW      = "workflow"
    EXTERNAL      = "external"


@dataclass(frozen=True)
class TransferTrigger:
    """One trigger producer's report of "something happened that might
    warrant a transfer" — never a transfer request itself. Exactly one of
    directive/violation_count/workflow_reason/external_reason is
    meaningful, selected by `type`.

    directive        — set for LLM_DIRECTIVE: the parsed [[TRANSFER]] tag.
    violation_count   — set for ESCALATION: GuardrailCounter's current
                        consecutive count (engine only compares it to
                        DecisionContext.escalation_threshold — it never
                        increments/resets/stores this itself).
    workflow_reason   — set for WORKFLOW: a short machine-readable label
                        ("account_locked", "payment_dispute", ...) from a
                        future WorkflowEngine.request_transfer(reason) —
                        no WorkflowEngine exists in this codebase yet; this
                        trigger type exists so one can be added later
                        without touching the engine.
    external_reason   — set for EXTERNAL: reserved for a future REST/
                        admin-triggered transfer (engine.request_transfer()
                        integration point) — no endpoint is built in this
                        phase.
    """
    type: TriggerType
    directive: TransferDirective | None = None
    violation_count: int | None = None
    workflow_reason: str | None = None
    external_reason: str | None = None


@dataclass(frozen=True)
class DecisionContext:
    """Everything evaluate() needs, gathered by the caller from wherever it
    actually lives (RuntimeConfig.policies, the caller's own per-session
    bookkeeping) — the engine reads this, never reaches out for it.

    already_requested — true once this session has already produced an
    accepted Decision that hasn't been superseded. This is the caller's
    (pipeline.py's) own bookkeeping, not a second FSM: the real
    Transferring/Finalizing/Closed states live in ConversationFSM
    (fsm.py), which the pipeline handler has no visibility into today (it
    only sees speech_ended calls, which stop arriving once a session
    closes — so "session already closed" needs no separate flag here: a
    closed session simply never calls evaluate() again). already_requested
    exists solely to catch the one real gap: two triggers (e.g. an LLM
    directive and an escalation breach) firing before the first transfer
    resolves.
    """
    session_id: str
    tenant_id: str
    call_id: str
    transfer_type: str                # RuntimeConfig.policies.transfer_type: "none"/"cold"/"warm"
    transfer_destination: str | None
    escalation_threshold: int | None
    already_requested: bool = False
    # Caller-ID resolution inputs — mirrors RuntimeConfig.policies.
    # caller_id_policy/platform_did/custom_caller_id, plus the caller's own
    # ANI (SessionContext.caller_did, known per-session, not part of
    # RuntimeConfig). See _resolve_caller_id() below. Only meaningful for
    # warm transfer — cold has no equivalent (uuid_transfer never
    # originates a new leg).
    caller_id_policy: str = "original"
    platform_did: str | None = None
    custom_caller_id: str | None = None
    caller_number: str = ""
    # RuntimeConfig.policies.transfer_waiting_experience — passed through
    # unresolved (see TransferRequest.waiting_experience's own comment).
    waiting_experience: str = "announcement_moh"


@dataclass(frozen=True)
class Decision:
    accepted: bool
    request: TransferRequest | None = None
    rejection_reason: str | None = None


def _resolve_caller_id(ctx: DecisionContext) -> str:
    """What caller ID the human agent should see, per agent.caller_id_
    policy. 'original' (the default) and any unrecognized/misconfigured
    policy value both fall back to the caller's own ANI — same degrade-
    safely posture the rest of this codebase's config handling uses,
    rather than originating a leg with no caller ID at all."""
    if ctx.caller_id_policy == "platform" and ctx.platform_did:
        return ctx.platform_did
    if ctx.caller_id_policy == "custom" and ctx.custom_caller_id:
        return ctx.custom_caller_id
    return ctx.caller_number


class TransferDecisionEngine:
    """Stateless — see module docstring. Every method is effectively a
    staticmethod; instances carry nothing but the (optional) metrics sink,
    which is itself just forwarded, not accumulated."""

    def __init__(self, metrics: IMetrics | None = None) -> None:
        self._metrics = metrics if metrics is not None else NullMetrics()

    def evaluate(self, ctx: DecisionContext, trigger: TransferTrigger) -> Decision:
        self._metrics.increment(f"transfer_decision_total.{ctx.tenant_id}")

        def reject(rejection_reason: str) -> Decision:
            log.info(
                "Transfer decision rejected trigger=%s reason=%s tenant_id=%s "
                "session_id=%s call_id=%s",
                trigger.type.value, rejection_reason, ctx.tenant_id,
                ctx.session_id, ctx.call_id,
            )
            self._metrics.increment(f"transfer_decision_rejected_total.{ctx.tenant_id}.{rejection_reason}")
            if rejection_reason == "already_transferring":
                self._metrics.increment(f"transfer_duplicate_total.{ctx.tenant_id}")
            return Decision(accepted=False, rejection_reason=rejection_reason)

        # Duplicate protection — at most one accepted Decision per session
        # for the lifetime this engine can observe (see DecisionContext's
        # already_requested docstring for why this, not a real FSM check,
        # is what's available here).
        if ctx.already_requested:
            return reject("already_transferring")

        if trigger.type is TriggerType.LLM_DIRECTIVE:
            if trigger.directive is None:
                return reject("missing_directive_payload")
            transfer_reason = TransferReason.CUSTOMER_REQUEST
            transfer_type   = trigger.directive.transfer_type
            destination     = trigger.directive.destination
            free_reason     = trigger.directive.reason

        elif trigger.type is TriggerType.ESCALATION:
            if trigger.violation_count is None:
                return reject("missing_violation_count")
            self._metrics.observe(
                f"escalation_counter_current.{ctx.tenant_id}", float(trigger.violation_count),
            )
            if ctx.escalation_threshold is None or trigger.violation_count <= ctx.escalation_threshold:
                return reject("threshold_not_reached")
            transfer_reason = TransferReason.ESCALATION_THRESHOLD
            transfer_type   = coerce_transfer_type(ctx.transfer_type)
            destination     = ctx.transfer_destination or ""
            free_reason     = f"escalation_threshold_exceeded (violations={trigger.violation_count})"

        elif trigger.type is TriggerType.WORKFLOW:
            transfer_reason = TransferReason.WORKFLOW_POLICY
            transfer_type   = coerce_transfer_type(ctx.transfer_type)
            destination     = ctx.transfer_destination or ""
            free_reason     = trigger.workflow_reason or "workflow_policy"

        elif trigger.type is TriggerType.EXTERNAL:
            transfer_reason = TransferReason.SYSTEM_REQUEST
            transfer_type   = coerce_transfer_type(ctx.transfer_type)
            destination     = ctx.transfer_destination or ""
            free_reason     = trigger.external_reason or "system_request"

        else:  # pragma: no cover — exhaustive over TriggerType
            return reject(f"unknown_trigger_type:{trigger.type!r}")

        if transfer_type == TransferType.NONE:
            return reject("transfer_disabled")
        if not destination:
            return reject("no_destination_configured")

        # Harmless to resolve unconditionally even for cold transfer — the
        # gateway simply never reads caller_id outside WarmTransferCoordinator.
        request = TransferRequest(
            session_id=ctx.session_id, tenant_id=ctx.tenant_id, call_id=ctx.call_id,
            transfer_type=transfer_type, destination=destination, reason=free_reason,
            trigger=transfer_reason.wire_trigger, caller_id=_resolve_caller_id(ctx),
            waiting_experience=ctx.waiting_experience,
        )
        log.info(
            "Transfer decision accepted trigger=%s type=%s destination=%s "
            "transfer_id=%s tenant_id=%s session_id=%s call_id=%s",
            transfer_reason.wire_trigger, transfer_type.value, destination,
            request.transfer_id, ctx.tenant_id, ctx.session_id, ctx.call_id,
        )
        self._metrics.increment(f"transfer_decision_accepted_total.{ctx.tenant_id}")
        return Decision(accepted=True, request=request)
