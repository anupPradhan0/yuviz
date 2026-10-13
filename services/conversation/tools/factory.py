"""One place that decides what a per-call ToolCallOrchestrator shares with every other call.

An orchestrator is built per call, so a breaker or last-good cache it owned would die with the call and
never see enough failures to open. The factory owns ONE of each and hands them to every orchestrator.
"""

from __future__ import annotations

from typing import Callable

from .executor_registry import ExecutorRegistry
from .guard import LastGoodCache
from .llm_adapter import LLMAdapter
from .middleware import BreakerBoard
from .orchestrator import ToolCallOrchestrator
from .policy_resolver import ToolPolicyResolver
from .provider_manager import ToolProviderManager
from ..tool_latency import ToolLatencyStore


def orchestrator_factory(
    *,
    policy_resolver: ToolPolicyResolver,
    provider_manager: ToolProviderManager,
    executor_registry: ExecutorRegistry,
    latency_store: ToolLatencyStore | None = None,
    breakers: BreakerBoard | None = None,
    last_good: LastGoodCache | None = None,
) -> Callable[[LLMAdapter], ToolCallOrchestrator]:
    shared_breakers = breakers if breakers is not None else BreakerBoard()
    shared_last_good = last_good if last_good is not None else LastGoodCache()

    def build(llm_adapter: LLMAdapter) -> ToolCallOrchestrator:
        return ToolCallOrchestrator(
            llm_adapter=llm_adapter,
            policy_resolver=policy_resolver,
            provider_manager=provider_manager,
            executor_registry=executor_registry,
            latency_store=latency_store,
            breakers=shared_breakers,
            last_good=shared_last_good,
        )

    return build
