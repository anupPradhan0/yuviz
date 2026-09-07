"""
WorkflowRunner — which node is active. No audio/providers (dry-run friendly).

Outgoing edges are local LLM tools; calling one advances the node.
Constructed per call with the handler — plain attrs, no session map.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from libs.config_sdk import RuntimeConfig
from libs.config_sdk.workflow import (
    ENDED_EARLY, Edge, Node, WorkflowGraph, WorkflowInvalid, parse_graph, render,
    starter_graph,
)

from ..providers.interfaces import ChatMessage
from ..tools.types import ToolDefinition, ToolResult, ToolStatus

log = logging.getLogger(__name__)

_NO_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}}
_TRANSITION_RESULT = ToolResult(status=ToolStatus.SUCCESS, payload={"status": "done"})


class WorkflowRunner:
    def __init__(
        self,
        graph: WorkflowGraph,
        *,
        base_suffix: str = "",
        variables: dict[str, Any] | None = None,
        extractor: Any | None = None,
        summarizer: Any | None = None,
    ) -> None:
        self._graph = graph
        self._node = graph.start
        self._global = graph.global_prompt
        self._suffix = base_suffix
        self._vars: dict[str, Any] = dict(variables or {})
        self._extractor = extractor
        self._summarizer = summarizer
        self.visited: list[str] = [self._node.name]
        # Pipeline reads/clears these after each turn (speech / hangup / transfer).
        self.pending_speech: str | None = None
        self.pending_end: bool = False
        self.pending_transfer: Node | None = None
        self.ended_off_graph: bool = False
        self.last_transition: str = ""

    @property
    def node(self) -> Node:
        return self._node

    @property
    def variables(self) -> dict[str, Any]:
        return dict(self._vars)

    def update_variables(self, values: dict[str, Any]) -> None:
        self._vars.update({k: v for k, v in values.items() if v is not None})

    def system_prompt(self) -> str:
        parts = [
            self.render(self._global),
            self.render(self._node.prompt),
            self._suffix,
        ]
        return "\n\n".join(p.strip() for p in parts if p and p.strip())

    def allowed_tool_names(self) -> list[str]:
        """Node tool allow-list for ToolPolicyResolver `only`. [] = no DB tools."""
        return list(self._node.tools)

    def knowledge_enabled(self) -> bool:
        # ponytail: per-KB filtering needs RetrievalPolicy.knowledge_base_ids + knowledge filter.
        return bool(self._node.knowledge_base_ids)

    def greeting(self) -> str | None:
        text = self.render(self._graph.start.greeting or "")
        return text or None

    @property
    def delayed_start_ms(self) -> int:
        return self._graph.start.delayed_start_ms

    @property
    def disposition(self) -> str | None:
        """End-node code, or ENDED_EARLY if [[END_CALL]] left a non-terminal node."""
        if self.ended_off_graph and not self._node.is_terminal:
            return ENDED_EARLY
        return self._node.disposition

    def render(self, text: str) -> str:
        return render(text, self._vars)

    def local_tools(
        self,
        turn: list[ChatMessage] | None = None,
        store: list[ChatMessage] | None = None,
    ) -> dict[str, tuple[ToolDefinition, Callable[[dict[str, Any]], Awaitable[ToolResult]]]]:
        """One local tool per outgoing edge. `turn`/`store` may differ when RAG
        built a throwaway copy — prompt swap must hit both; extract/summarize only `store`."""
        tools: dict[str, tuple[ToolDefinition, Callable[..., Awaitable[ToolResult]]]] = {}
        for edge in self._node.out_edges:
            definition = ToolDefinition(
                name=edge.tool_name,
                description=edge.condition,
                parameters_schema=_NO_PARAMETERS,
                category="workflow_transition",
            )

            def handler(_args: dict[str, Any], _edge: Edge = edge) -> Awaitable[ToolResult]:
                return self._transition(_edge, turn, store if store is not None else turn)

            tools[edge.tool_name] = (definition, handler)
        return tools

    async def _transition(
        self,
        edge: Edge,
        turn: list[ChatMessage] | None,
        store: list[ChatMessage] | None,
    ) -> ToolResult:
        source = self._node

        if self._extractor is not None and source.extraction is not None and source.extraction.enabled:
            self._extractor.extract(source, store or [])

        self.pending_speech = self.render(edge.transition_speech or "") or None
        self._node = self._graph.nodes[edge.target]
        self.last_transition = edge.tool_name
        self.visited.append(self._node.name)
        log.info(
            "workflow: %s --%s--> %s", source.name, edge.tool_name, self._node.name,
        )

        if self._node.type == "end":
            self.pending_end = True
        elif self._node.type == "transfer":
            self.pending_transfer = self._node

        # Swap prompt mid-turn so the rest of this generate uses the new node.
        prompt: str | None = None
        for messages in (turn, store):
            if not messages:
                continue
            if prompt is None:
                prompt = self.system_prompt()
            if messages[0].role == "system":
                messages[0] = ChatMessage(role="system", content=prompt)
            else:
                messages.insert(0, ChatMessage(role="system", content=prompt))
            if messages is turn and store is turn:
                break

        if store and self._summarizer is not None:
            self._summarizer.maybe_summarize(store)

        return _TRANSITION_RESULT


_GRAPH_CACHE: dict[tuple[str, int, bool], WorkflowGraph] = {}


def graph_for(runtime_config: RuntimeConfig, *, draft: bool = False) -> WorkflowGraph:
    """Never raises. Missing/bad published graph → starter seeded from column
    greeting/system_prompt (until PR11 drops those columns).

    draft=True prefers workflow_draft; invalid draft falls back to published.
    Callers that want draft testing must pass draft=True (admin test-call path;
    not wired on SessionOpenRequest yet — PR10).
    """
    raw = runtime_config.conversation.workflow
    if draft and runtime_config.conversation.workflow_draft:
        raw = runtime_config.conversation.workflow_draft
    if not raw:
        if not draft:
            log.error(
                "workflow: agent %s has no published graph — running starter "
                "seeded from greeting/system_prompt",
                runtime_config.agent.slug,
            )
        return _fallback_graph(runtime_config)
    # Drafts uncached: draft save does not bump config_version.
    key = (runtime_config.agent.id or runtime_config.agent.slug, runtime_config.version, draft)
    if not draft and key in _GRAPH_CACHE:
        return _GRAPH_CACHE[key]
    try:
        graph = parse_graph(raw)
    except WorkflowInvalid as exc:
        if draft:
            log.info(
                "workflow: draft for agent %s does not parse (%s) — using published",
                runtime_config.agent.slug, exc,
            )
            return graph_for(runtime_config, draft=False)
        log.error(
            "workflow: agent %s published graph does not parse (%s) — starter fallback",
            runtime_config.agent.slug, exc,
        )
        graph = _fallback_graph(runtime_config)
    except Exception:
        log.exception("workflow: unexpected parse failure for agent %s", runtime_config.agent.slug)
        graph = _fallback_graph(runtime_config)
    if not draft:
        _GRAPH_CACHE[key] = graph
    return graph


def _fallback_graph(runtime_config: RuntimeConfig) -> WorkflowGraph:
    return parse_graph(starter_graph(
        runtime_config.conversation.greeting or "",
        runtime_config.conversation.system_prompt or "",
    ))
