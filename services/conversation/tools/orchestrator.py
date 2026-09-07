"""
ToolCallOrchestrator — the only component that talks to both halves of the
framework (LLMAdapter on the LLM-facing side, ToolPolicyResolver/
ExecutorRegistry/ToolProviderManager on the execution side). See the Tool
Execution Framework design for the full architecture; this class is
run_turn()'s implementation of §06's lifecycle end to end.

Deliberately provider-agnostic: nothing here mentions "calendar," "Cal.com,"
or any specific tool by name — swapping CalendarExecutor for a CRM or
ticketing executor tomorrow changes zero lines here.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Awaitable, Callable

from ..metrics import IMetrics
from ..providers.interfaces import ChatMessage
from .executor_registry import ExecutorRegistry
from .llm_adapter import (
    DeterministicSpokenEvent,
    LLMAdapter,
    LocalToolCompletedEvent,
    TokenEvent,
    ToolCallEvent,
    ToolCallStartedEvent,
    TurnEvent,
)
from .middleware import build_default_chain
from .policy_resolver import ToolPolicyResolver
from .provider_manager import ToolProviderManager
from .types import ToolDefinition, ToolExecutionContext, ToolExecutionRequest, ToolResult, ToolStatus

log = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT_MS = 6000
DEFAULT_MAX_TOOL_ITERATIONS = 2
# Local tools don't count against DEFAULT_MAX_TOOL_ITERATIONS (they cost no
# round-trip), but they still need a ceiling of their own: a workflow may
# legally contain a cycle, so a model that keeps calling transitions would
# otherwise walk A->B->A forever INSIDE one turn — and max_call_duration_s
# can't catch that, because it's checked between turns and the turn never
# ends. Past the cap they stop being offered; the turn finishes in words.
DEFAULT_MAX_LOCAL_TOOL_CALLS = 8

# A tool that executes in this process, offered to the LLM alongside the
# DB-backed policy tools but resolved by neither ToolPolicyResolver nor
# ToolProviderManager: it has no policy row, no credentials and no circuit
# breaker, because it cannot fail in the ways those exist to handle.
# Workflow transitions (see services/conversation/workflow/runner.py) are
# the first user; nothing here knows that — same provider-agnostic posture
# as the rest of this module.
LocalTools = dict[str, tuple[ToolDefinition, Callable[[dict[str, Any]], Awaitable[ToolResult]]]]
# A caller whose tool set can change DURING a turn passes a callable instead
# of a dict — see run_turn's `local_tools`.
LocalToolsSource = "LocalTools | Callable[[], LocalTools] | None"


class ToolCallOrchestrator:
    def __init__(
        self,
        llm_adapter:       LLMAdapter,
        policy_resolver:   ToolPolicyResolver,
        provider_manager:  ToolProviderManager,
        executor_registry: ExecutorRegistry,
        metrics:           IMetrics | None = None,
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
        max_local_tool_calls: int = DEFAULT_MAX_LOCAL_TOOL_CALLS,
    ) -> None:
        self._llm_adapter = llm_adapter
        self._policy_resolver = policy_resolver
        self._provider_manager = provider_manager
        self._executor_registry = executor_registry
        self._metrics = metrics
        self._max_tool_iterations = max_tool_iterations
        self._max_local_tool_calls = max_local_tool_calls

    async def run_turn(
        self, agent_id: str, tenant_id: str, call_id: str, session_id: str, history: list[ChatMessage],
        caller_number: str = "", cancel_event: "asyncio.Event | None" = None,
        force_tool_name: str | None = None, phone_number_confirmed: bool = False,
        local_tools: Any = None,
        only_tools: "list[str] | Callable[[], list[str] | None] | None" = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        """only_tools narrows the agent's DB-backed tool set for this turn
        (a workflow node's `tools` list); None leaves it as resolved. It can
        only ever remove — a node cannot grant a tool the agent doesn't
        have, which would be privilege escalation through the graph editor.

        Both may be passed as zero-argument callables instead of values, for
        a caller whose tool set changes DURING a turn: executing a local tool
        can move a workflow to a different node, and every generation after
        that must be offered the NEW node's tools. Passing plain values (the
        single-prompt case, where nothing can change mid-turn) resolves them
        once and re-reads the same thing.
        """
        local_calls = 0

        async def resolve() -> tuple[LocalTools, dict, list[dict] | None, list[dict]]:
            local = local_tools() if callable(local_tools) else (local_tools or {})
            if local_calls >= self._max_local_tool_calls:
                local = {}   # see DEFAULT_MAX_LOCAL_TOOL_CALLS
            only = only_tools() if callable(only_tools) else only_tools
            policies = await self._policy_resolver.enabled_tools(agent_id, only=only)
            # llm_visible=False tools (e.g. send_sms) are admin-configurable but
            # never offered to the model — see ToolDefinition's own docstring.
            llm_schemas = [
                p.definition.to_generic_schema() for p in policies if p.definition.llm_visible
            ]
            local_schemas = [d.to_generic_schema() for d, _ in local.values()]
            schemas = llm_schemas + local_schemas
            return local, {p.definition.name: p for p in policies}, (schemas or None), local_schemas

        local, policies_by_name, schemas, local_schemas = await resolve()

        turn_id = str(uuid.uuid4())
        iteration = 0
        # Only forced on this turn's very first generate() call — see
        # pipeline.py's _message_reads_back_phone_number for the one
        # narrow condition that sets force_tool_name at all (right after
        # the caller confirms their phone number). Never re-forced on a
        # later iteration within the same turn: once a tool call has
        # already happened once, iterate normally rather than coercing a
        # second forced call the model may have no reason to make.
        tool_choice = (
            {"type": "function", "function": {"name": force_tool_name}}
            if force_tool_name else None
        )

        while True:
            tool_call_happened = False
            this_call_tool_choice, tool_choice = tool_choice, None
            async for event in self._llm_adapter.generate(history, schemas, tool_choice=this_call_tool_choice):
                if isinstance(event, TokenEvent):
                    yield event
                    continue

                assert isinstance(event, ToolCallEvent)
                tool_call_happened = True

                local_entry = local.get(event.tool_name)
                if local_entry is None:
                    # The iteration cap guards runaway *external* calls; an
                    # in-process handler is not what it's for. Counting one
                    # would burn a turn's whole budget on a pointer move and
                    # leave the next generation with its tools yanked.
                    iteration += 1
                    # Only an external round-trip is slow enough to need a
                    # spoken filler (see pipeline.py's _TOOL_CALL_FILLER).
                    yield ToolCallStartedEvent(tool_name=event.tool_name)
                    result = await self._execute_tool_call(
                        event, policies_by_name, tenant_id, agent_id, call_id, session_id, turn_id,
                        iteration, caller_number, cancel_event, phone_number_confirmed,
                    )
                else:
                    local_calls += 1
                    result = await local_entry[1](event.arguments)
                    # Before the next generate() — that round-trip is the
                    # gap the caller would otherwise hear as dead air.
                    yield LocalToolCompletedEvent(tool_name=event.tool_name)
                    # A local tool can have moved a workflow to another node,
                    # whose prompt the rest of this turn already runs under
                    # (see WorkflowRunner._transition). Re-resolve so it runs
                    # under that node's TOOLS too — otherwise the model is
                    # told to book an appointment while still holding the
                    # previous stage's tool list, and the edges it just left
                    # stay callable, which would let it take a transition the
                    # validated graph doesn't have.
                    local, policies_by_name, schemas, local_schemas = await resolve()
                _fold_tool_result_into_history(history, event, result)
                if result.deterministic_response is not None:
                    # This exact outcome must reach the caller verbatim —
                    # see ToolResult.deterministic_response's own docstring.
                    # No further LLM generate() call for this turn: the
                    # words were never the LLM's to choose, so there is
                    # nothing left for it to narrate.
                    yield DeterministicSpokenEvent(
                        text=result.deterministic_response, confirmed_datetime=result.confirmed_datetime,
                    )
                    return
                if result.status == ToolStatus.FAILED and result.error == "cancelled":
                    # The caller barged in while this tool call was still in
                    # flight — see _execute_tool_call's own comment on why we
                    # stop waiting rather than abort the request itself. The
                    # turn is stale the moment this happens: continuing to
                    # iterate (asking the LLM what to do next) would just
                    # generate a response to a turn the caller has already
                    # moved on from, exactly the dead-turn-that-still-talks
                    # problem cancellation is supposed to prevent.
                    return
                break  # a tool call ends this generate() pass — restart with updated history

            if not tool_call_happened:
                return  # plain-text turn completed normally, no further iteration

            if iteration >= self._max_tool_iterations:
                # Force closure: one final generation with no *external*
                # tools offered, guaranteeing natural-language wrap-up
                # instead of a silent stall (see design §06). Local tools
                # survive it — withdrawing a workflow's transitions here
                # would strand the conversation in a node it had already
                # decided to leave, and they are not what ran away.
                schemas = local_schemas or None

    async def _execute_tool_call(
        self, event: ToolCallEvent, policies_by_name: dict, tenant_id: str, agent_id: str,
        call_id: str, session_id: str, turn_id: str, iteration: int, caller_number: str = "",
        cancel_event: "asyncio.Event | None" = None, phone_number_confirmed: bool = False,
    ) -> ToolResult:
        policy = policies_by_name.get(event.tool_name)
        if policy is None:
            log.warning("ToolCallOrchestrator: LLM called unoffered tool_name=%r", event.tool_name)
            return ToolResult(status=ToolStatus.FAILED, error="unknown_tool")

        try:
            provider = await self._provider_manager.get(policy)
        except Exception:
            log.exception("ToolCallOrchestrator: provider construction failed tool=%s", event.tool_name)
            return ToolResult(status=ToolStatus.FAILED, error="provider_unavailable")

        # Generic — this orchestrator never hardcodes a tool name (see
        # module docstring); it only follows whatever companion_tool_name
        # the resolved tool's own definition declares, if any.
        companion = None
        companion_tool_name = policy.definition.companion_tool_name
        if companion_tool_name is not None:
            companion_policy = policies_by_name.get(companion_tool_name)
            if companion_policy is not None:
                try:
                    companion = await self._provider_manager.get(companion_policy)
                except Exception:
                    log.exception(
                        "ToolCallOrchestrator: companion provider construction failed tool=%s companion=%s",
                        event.tool_name, companion_tool_name,
                    )

        executor = self._executor_registry.resolve(event.tool_name, provider, companion)
        if executor is None:
            log.error("ToolCallOrchestrator: no executor registered for tool_name=%r", event.tool_name)
            return ToolResult(status=ToolStatus.FAILED, error="no_executor_registered")

        timeout_ms = policy.timeout_ms or DEFAULT_TOOL_TIMEOUT_MS
        chain = build_default_chain(executor, timeout_ms=timeout_ms, metrics=self._metrics)

        request = ToolExecutionRequest(
            tool_call_id=event.tool_call_id,
            tool_name=event.tool_name,
            arguments=event.arguments,
            context=ToolExecutionContext(
                tenant_id=tenant_id, agent_id=agent_id, call_id=call_id, session_id=session_id,
                turn_id=turn_id, tool_iteration=iteration,
                deadline=time.monotonic() + timeout_ms / 1000,
                request_id=str(uuid.uuid4()),
                caller_number=caller_number,
                phone_number_confirmed=phone_number_confirmed,
            ),
        )

        if cancel_event is None:
            return await chain.execute(request)

        # Cancellation-on-interruption (adopted from pipecat's
        # FunctionCallInProgressFrame pattern, 2026-08-02): without this, a
        # barge-in during a slow tool call (a real Cal.com round-trip, up to
        # timeout_ms) was silently swallowed — the pipeline's own cancel
        # check couldn't act until this await returned, so the caller's
        # interruption was ignored for as long as 6+ seconds. Race the tool
        # execution against the cancel signal instead of just awaiting it;
        # if the caller barges in first, stop *waiting* on the result
        # immediately rather than aborting the in-flight request itself — a
        # half-sent Cal.com POST could still land server-side, and killing
        # our side's wait doesn't un-book or un-cancel anything real, it
        # just stops this stale turn from continuing to talk about it.
        execute_task = asyncio.ensure_future(chain.execute(request))
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        try:
            done, _ = await asyncio.wait({execute_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
            if execute_task in done:
                return execute_task.result()
            log.info(
                "ToolCallOrchestrator: caller interrupted mid tool_call=%r — no longer waiting on its result",
                event.tool_name,
            )
            return ToolResult(status=ToolStatus.FAILED, error="cancelled")
        finally:
            cancel_task.cancel()
            if not execute_task.done():
                # Let it keep running in the background rather than cancel
                # it (see comment above) — but still consume its eventual
                # result/exception so it doesn't surface as an "exception
                # never retrieved" warning with nothing to hand it to.
                execute_task.add_done_callback(_log_background_tool_result)


def _log_background_tool_result(task: "asyncio.Task[ToolResult]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("ToolCallOrchestrator: backgrounded tool call (post-cancel) failed: %r", exc)


def _fold_tool_result_into_history(history: list[ChatMessage], event: ToolCallEvent, result: ToolResult) -> None:
    """Appends the assistant's tool call and the tool's result as two new
    ChatMessages, in the generic shape every IToolAwareLLM implementation
    bridges into its own native wire format (see ollama.py/gemini.py)."""
    import json

    call_dict: dict[str, Any] = {"id": event.tool_call_id, "name": event.tool_name, "arguments": event.arguments}
    if event.provider_metadata:
        call_dict["provider_metadata"] = event.provider_metadata
    history.append(ChatMessage(role="assistant", content="", tool_calls=[call_dict]))
    payload: dict[str, Any] = {"status": result.status.value, **result.payload}
    if result.error:
        payload["error"] = result.error
    history.append(ChatMessage(
        role="tool", content=json.dumps(payload), tool_call_id=event.tool_call_id,
    ))
