"""WorkflowRunner — which node is active (docs/workflow.md §5.2). No audio/providers."""

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

# Explicit empty schema: providers differ on a missing `parameters` key (llm_adapter).
_NO_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}}

# Minimal identical result; extractor strips these (noise if they accumulate).
_TRANSITION_RESULT = ToolResult(status=ToolStatus.SUCCESS, payload={"status": "done"})


def _transition_tool_name(edge: Edge) -> str:
    """Prefix so edge labels cannot shadow ToolRegistry names (book_appointment…)."""
    return f"goto_{edge.tool_name}"


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
        self._suffix = base_suffix  # date + [[END_CALL]] instruction
        self._vars: dict[str, Any] = dict(variables or {})
        self._extractor = extractor
        self._summarizer = summarizer
        self.visited: list[str] = [self._node.name]
        # Pipeline reads/clears after each turn; we only flag, never act.
        self.pending_speech: str | None = None
        self.pending_end: bool = False
        self.pending_transfer: Node | None = None
        # Set when [[END_CALL]] fired off a non-terminal node (see disposition).
        self.ended_off_graph: bool = False
        self.last_transition: str = ""
        # Node left on transfer enter — abandon_transfer() reverts here.
        self._pre_transfer_node: Node | None = None

    @property
    def node(self) -> Node:
        return self._node

    @property
    def variables(self) -> dict[str, Any]:
        return dict(self._vars)

    def update_variables(self, values: dict[str, Any]) -> None:
        """Merge extraction results into prompt variables."""
        self._vars.update({k: v for k, v in values.items() if v is not None})

    def extracted_variables(self) -> dict[str, Any]:
        """Values produced by extraction only — not seeded call-context keys."""
        declared = self._graph.declared_variables()
        return {k: v for k, v in self._vars.items() if k in declared}

    def system_prompt(self) -> str:
        """Compose global + node + suffix; re-render so earlier extractions vars appear."""
        parts = [
            self.render(self._global),
            self.render(self._node.prompt),
            self._suffix,
        ]
        return "\n\n".join(p.strip() for p in parts if p and p.strip())

    def allowed_tool_names(self) -> list[str] | None:
        """ToolPolicyResolver `only`: None = do not narrow (starter); [] = deny all."""
        if self._graph.is_single_stage:
            return None
        return list(self._node.tools)

    def knowledge_enabled(self) -> bool:
        """RAG on/off. Single-stage / all-empty keep agent-level RAG; else per-node ids."""
        if self._node.knowledge_base_ids:
            return True
        if self._graph.is_single_stage:
            return True
        return not any(n.knowledge_base_ids for n in self._graph.nodes.values())

    def greeting(self) -> str | None:
        """Start-node greeting (wins over any legacy agent greeting)."""
        text = self.render(self._graph.start.greeting or "")
        return text or None

    @property
    def delayed_start_ms(self) -> int:
        return self._graph.start.delayed_start_ms

    @property
    def disposition(self) -> str | None:
        """End-node code, or ENDED_EARLY if [[END_CALL]] mid-graph (vs caller hangup)."""
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
        """One transition tool per outgoing edge. Swap prompt mid-turn on turn+store
        (RAG copies differ; between-turns is too late — see docs/workflow.md §5.3)."""
        tools: dict[str, tuple[ToolDefinition, Callable[..., Awaitable[ToolResult]]]] = {}
        for edge in self._node.out_edges:
            name = _transition_tool_name(edge)
            definition = ToolDefinition(
                name=name,
                description=edge.condition,
                parameters_schema=_NO_PARAMETERS,
                category="workflow_transition",
            )

            def handler(_args: dict[str, Any], _edge: Edge = edge) -> Awaitable[ToolResult]:
                return self._transition(_edge, turn, store if store is not None else turn)

            tools[name] = (definition, handler)
        return tools

    def abandon_transfer(self) -> None:
        """Move off a rejected transfer node so the caller is not dead-ended."""
        if self._pre_transfer_node is None or self._node.type != "transfer":
            self.pending_transfer = None
            return
        source = self._pre_transfer_node
        self._node = source
        self._pre_transfer_node = None
        self.pending_transfer = None
        if self.visited and self.visited[-1] != source.name:
            self.visited.pop()
        log.info("workflow: transfer rejected — reverted to %s", source.name)

    async def _transition(
        self,
        edge: Edge,
        turn: list[ChatMessage] | None,
        store: list[ChatMessage] | None,
    ) -> ToolResult:
        source = self._node

        # Queue extract before leave — pipeline runs it after live generate.
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
            self._pre_transfer_node = None
        elif self._node.type == "transfer":
            self.pending_transfer = self._node
            self._pre_transfer_node = source
        else:
            self._pre_transfer_node = None

        # Mid-turn prompt swap on both lists when they differ (see local_tools).
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
                break  # same object, don't swap twice

        # Only persistent history — RAG turn copy is throwaway.
        if store and self._summarizer is not None:
            self._summarizer.maybe_summarize(store)

        return _TRANSITION_RESULT


# One entry per (tenant, agent); version stored beside the graph. Cap bounds fleet size.
_GRAPH_CACHE_MAX = 256
_GRAPH_CACHE: dict[tuple[str, str], tuple[int, WorkflowGraph]] = {}


def _cache_key(runtime_config: RuntimeConfig) -> tuple[str, str]:
    agent = runtime_config.agent.id or runtime_config.agent.slug or ""
    tenant = runtime_config.tenant.id or runtime_config.tenant.slug or ""
    return (tenant, agent)


def _cache_get(runtime_config: RuntimeConfig) -> WorkflowGraph | None:
    item = _GRAPH_CACHE.get(_cache_key(runtime_config))
    if item is None:
        return None
    version, graph = item
    if version != runtime_config.version:
        return None
    return graph


def _cache_put(runtime_config: RuntimeConfig, graph: WorkflowGraph) -> None:
    key = _cache_key(runtime_config)
    if key not in _GRAPH_CACHE and len(_GRAPH_CACHE) >= _GRAPH_CACHE_MAX:
        _GRAPH_CACHE.pop(next(iter(_GRAPH_CACHE)))
    _GRAPH_CACHE[key] = (runtime_config.version, graph)


def _str_names_from_raw(raw: dict[str, Any] | None, field: str) -> list[str]:
    """Best-effort scrape of node list fields from unparseable published JSON."""
    if not isinstance(raw, dict):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for node in raw.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        for item in data.get(field) or []:
            if isinstance(item, str) and item and item not in seen:
                seen.add(item)
                names.append(item)
    return names


def graph_for(runtime_config: RuntimeConfig, *, draft: bool = False) -> WorkflowGraph:
    """Never raises. Missing/bad published graph → starter seeded from tools.

    draft=True prefers workflow_draft; invalid draft falls back to published.
    """
    graph, _fell_back = resolve_graph(runtime_config, draft=draft)
    return graph


def resolve_graph(
    runtime_config: RuntimeConfig, *, draft: bool = False,
) -> tuple[WorkflowGraph, bool]:
    """Like graph_for, plus whether an invalid draft forced the published graph."""
    raw = runtime_config.conversation.workflow
    if draft and runtime_config.conversation.workflow_draft:
        raw = runtime_config.conversation.workflow_draft
    if not raw:
        if not draft:
            log.error(
                "workflow: agent %s has no published graph — running the starter "
                "graph; publish one from the editor",
                runtime_config.agent.slug,
            )
        return _fallback_graph(runtime_config, raw=None), False
    # Drafts not cached: draft save does not bump config_version.
    if not draft:
        cached = _cache_get(runtime_config)
        if cached is not None:
            return cached, False
    try:
        graph = parse_graph(raw)
    except WorkflowInvalid as exc:
        if draft:
            log.info(
                "workflow: draft for agent %s does not parse (%s) — testing the "
                "published graph instead", runtime_config.agent.slug, exc,
            )
            published, _ = resolve_graph(runtime_config, draft=False)
            return published, True
        log.error(
            "workflow: agent %s has a published graph that does not parse (%s) — "
            "running the starter graph; republish it from the editor",
            runtime_config.agent.slug, exc,
        )
        graph = _fallback_graph(runtime_config, raw=raw if isinstance(raw, dict) else None)
    except Exception:
        log.exception("workflow: unexpected failure parsing graph for agent %s", runtime_config.agent.slug)
        graph = _fallback_graph(runtime_config, raw=raw if isinstance(raw, dict) else None)
    if not draft:
        _cache_put(runtime_config, graph)
    return graph, False


def _fallback_graph(
    runtime_config: RuntimeConfig, *, raw: dict[str, Any] | None,
) -> WorkflowGraph:
    # Prefer RuntimeConfig.tools; scrape broken JSON so parse failure ≠ strip tools.
    tools = [t.name for t in runtime_config.tools] or _str_names_from_raw(raw, "tools")
    kb_ids = _str_names_from_raw(raw, "knowledge_base_ids")
    return parse_graph(starter_graph(
        runtime_config.conversation.greeting or "",
        runtime_config.conversation.system_prompt or "",
        tools,
        kb_ids,
    ))
