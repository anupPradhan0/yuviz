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
from typing import Any, AsyncGenerator

from ..metrics import IMetrics
from ..providers.interfaces import ChatMessage
from .executor_registry import ExecutorRegistry
from .llm_adapter import LLMAdapter, ToolCallEvent, ToolCallStartedEvent, TokenEvent, TurnEvent
from .middleware import build_default_chain
from .policy_resolver import ToolPolicyResolver
from .provider_manager import ToolProviderManager
from .types import ToolExecutionContext, ToolExecutionRequest, ToolResult, ToolStatus

log = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT_MS = 6000
DEFAULT_MAX_TOOL_ITERATIONS = 2


class ToolCallOrchestrator:
    def __init__(
        self,
        llm_adapter:       LLMAdapter,
        policy_resolver:   ToolPolicyResolver,
        provider_manager:  ToolProviderManager,
        executor_registry: ExecutorRegistry,
        metrics:           IMetrics | None = None,
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
    ) -> None:
        self._llm_adapter = llm_adapter
        self._policy_resolver = policy_resolver
        self._provider_manager = provider_manager
        self._executor_registry = executor_registry
        self._metrics = metrics
        self._max_tool_iterations = max_tool_iterations

    async def run_turn(
        self, agent_id: str, tenant_id: str, call_id: str, session_id: str, history: list[ChatMessage],
        caller_number: str = "", cancel_event: "asyncio.Event | None" = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        policies = await self._policy_resolver.enabled_tools(agent_id)
        policies_by_name = {p.definition.name: p for p in policies}
        schemas = [p.definition.to_generic_schema() for p in policies] if policies else None

        turn_id = str(uuid.uuid4())
        iteration = 0

        while True:
            tool_call_happened = False
            async for event in self._llm_adapter.generate(history, schemas):
                if isinstance(event, TokenEvent):
                    yield event
                    continue

                assert isinstance(event, ToolCallEvent)
                tool_call_happened = True
                iteration += 1

                yield ToolCallStartedEvent(tool_name=event.tool_name)

                result = await self._execute_tool_call(
                    event, policies_by_name, tenant_id, agent_id, call_id, session_id, turn_id, iteration,
                    caller_number, cancel_event,
                )
                _fold_tool_result_into_history(history, event, result)
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
                # Force closure: one final generation with no tools offered,
                # guaranteeing natural-language wrap-up instead of a silent
                # stall (see design §06).
                schemas = None

    async def _execute_tool_call(
        self, event: ToolCallEvent, policies_by_name: dict, tenant_id: str, agent_id: str,
        call_id: str, session_id: str, turn_id: str, iteration: int, caller_number: str = "",
        cancel_event: "asyncio.Event | None" = None,
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

        executor = self._executor_registry.resolve(event.tool_name, provider)
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
