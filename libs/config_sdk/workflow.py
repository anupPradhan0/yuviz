"""
Workflow graph model — the single definition both planes share (see
docs/workflow.md §5.1). Config Service validates a graph here on publish;
Conversation Service walks the same parsed object at runtime. Dataclass
only, no pydantic: this SDK is dataclass-only today and gains no
dependency for this.

A graph is a state machine over a call. One node is active at a time; it
owns the system prompt and the tools reachable while it is active. Each
outgoing edge is registered with the LLM as a callable function whose name
is the edge label and whose description is the edge condition — the model
advances the conversation by calling it (see services/conversation/workflow/).

parse_graph() raises rather than returning a partial graph: every rule it
enforces is a runtime break (a dangling edge target is a KeyError mid-call,
a node with no outgoing edge traps the model until max_call_duration_s).
graph_warnings() is the other half — real editor mistakes that are not
runtime breaks (an unreachable node, a {{ variable }} nothing ever
defines), reported so the editor can surface them without blocking a
publish.

Cycles are allowed and deliberately not checked: "the caller has another
question" looping back to a Q&A node is correct behavior, and runaway
loops are already bounded by agents.max_call_duration_s.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

NODE_TYPES = ("start", "agent", "transfer", "end", "global")
TERMINAL_NODE_TYPES = ("end", "transfer")
# Not a step in the call — a `global` node carries the always-on instruction
# prepended to every step's prompt, and is deliberately wired to nothing. It
# is the one node type exempt from the connectivity rules below, so the two
# places that ask "is this node part of the flow?" ask it here rather than
# spelling out the type each time (ported from Dograh's NodeType.globalNode).
UNWIRED_NODE_TYPES = ("global",)

# Disposition codes the platform itself understands (ported from Dograh's
# disposition_codes.py, minus their telephony-status-derived ones). An end
# node may carry any string — the calls-list filter reads distinct values
# out of the column rather than a hardcoded list, so a tenant-invented code
# shows up without a code change. These are only the ones anything in the
# platform reasons about.
#
# Written by the runtime, never chosen in the editor: the agent ended the
# call with [[END_CALL]] from a node that was not an end node, so the graph
# reported no code of its own. Usually means the graph is missing an edge
# for however the conversation actually finished.
ENDED_EARLY = "ended_early"
SYSTEM_DISPOSITIONS = (
    "completed", "qualified", "not_qualified", "transferred", "abandoned", "failed",
    ENDED_EARLY,
)

# Always available to {{ }} rendering, supplied by the pipeline per call
# (see WorkflowRunner's `variables`). Anything outside this set must be
# declared by some node's extraction config or come from campaign contact
# data — graph_warnings() flags what neither covers.
CALL_CONTEXT_VARIABLES = (
    "caller_number", "called_number", "direction", "agent_name",
    "current_date", "current_time", "business_name",
)

# {{ name }} or {{ name | fallback text }}. Deliberately not Jinja2 (which
# is already a dependency): these strings are rendered with caller-
# influenced extracted values in them, and full Jinja on that is a
# sandbox-escape surface with no upside here.
_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\|([^}]*))?\}\}")


@dataclass(frozen=True)
class WorkflowError:
    """Structured so the editor can paint the offending node or edge red
    with the message attached, instead of one toast the operator has to go
    hunting from.

    `message` is written for the person drawing the flow, not for whoever
    wrote this file — it is rendered verbatim in the editor's problem list
    (admin-ui/components/workflow/WorkflowPanel.tsx), so it says what to do
    about it and avoids "node", "edge" and "LLM" in favour of the words the
    UI itself uses: step, connection, agent."""
    kind:    str          # "node" | "edge" | "workflow"
    id:      str | None
    field:   str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "field": self.field, "message": self.message}


class WorkflowInvalid(Exception):
    def __init__(self, errors: list[WorkflowError]) -> None:
        self.errors = errors
        super().__init__("; ".join(e.message for e in errors))


@dataclass(frozen=True)
class ExtractionVariable:
    name:   str
    type:   str = "string"   # string | number | boolean
    prompt: str = ""


@dataclass(frozen=True)
class Extraction:
    enabled:   bool = False
    prompt:    str = ""
    variables: tuple[ExtractionVariable, ...] = ()


@dataclass(frozen=True)
class Edge:
    id:                str
    source:            str
    target:            str
    label:             str
    condition:         str
    transition_speech: str | None = None

    @property
    def tool_name(self) -> str:
        """The label, as a function name the LLM can call. Two labels that
        collapse to the same name ("yes" and "Yes!") are rejected at
        validation — the second would silently shadow the first."""
        return re.sub(r"[^a-z0-9]+", "_", self.label.lower()).strip("_")


@dataclass
class Node:
    id:                   str
    type:                 str
    name:                 str
    prompt:               str = ""
    greeting:             str | None = None
    delayed_start_ms:     int = 0
    tools:                list[str] = field(default_factory=list)
    knowledge_base_ids:   list[str] = field(default_factory=list)
    extraction:           Extraction | None = None
    transfer_destination: str | None = None
    disposition:          str | None = None
    out_edges:            list[Edge] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.type in TERMINAL_NODE_TYPES

    @property
    def is_unwired(self) -> bool:
        return self.type in UNWIRED_NODE_TYPES


@dataclass
class WorkflowGraph:
    nodes:         dict[str, Node]
    start_node_id: str

    @property
    def start(self) -> Node:
        return self.nodes[self.start_node_id]

    @property
    def global_prompt(self) -> str:
        """The always-on instruction, prepended to every step's own prompt.
        Empty string when the graph has no global node — an agent whose
        every instruction is per-step is a legitimate graph, not an error."""
        for node in self.nodes.values():
            if node.is_unwired:
                return node.prompt
        return ""

    def reachable(self) -> set[str]:
        seen: set[str] = set()
        stack = [self.start_node_id]
        while stack:
            node_id = stack.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            stack.extend(e.target for e in self.nodes[node_id].out_edges)
        return seen

    def template_variables(self) -> set[str]:
        """Every {{ var }} referenced anywhere in the graph — prompts,
        greeting, transition speech."""
        found: set[str] = set()
        for node in self.nodes.values():
            for text in (node.prompt, node.greeting or ""):
                found |= template_variables(text)
            for edge in node.out_edges:
                found |= template_variables(edge.transition_speech or "")
        return found

    def declared_variables(self) -> set[str]:
        return {
            v.name
            for node in self.nodes.values()
            if node.extraction is not None
            for v in node.extraction.variables
        }


# ── Rendering ────────────────────────────────────────────────────────────

def template_variables(text: str) -> set[str]:
    return {m.group(1) for m in _TEMPLATE_RE.finditer(text or "")}


def render(text: str, variables: dict[str, Any]) -> str:
    """Substitute {{ name }} / {{ name | fallback }}. An unknown variable
    with no fallback renders as the empty string, never as a literal
    `{{ x }}` — that is the failure mode that ends up in a call recording."""
    if not text:
        return text or ""

    def _sub(m: re.Match[str]) -> str:
        value = variables.get(m.group(1))
        if value is None or value == "":
            return (m.group(2) or "").strip()
        return str(value)

    return _TEMPLATE_RE.sub(_sub, text)


# ── Parsing ──────────────────────────────────────────────────────────────

def _parse_extraction(raw: Any) -> Extraction | None:
    if not isinstance(raw, dict):
        return None
    variables = tuple(
        ExtractionVariable(
            name=str(v.get("name", "")).strip(),
            type=str(v.get("type", "string")),
            prompt=str(v.get("prompt", "")),
        )
        for v in raw.get("variables") or []
        if isinstance(v, dict) and str(v.get("name", "")).strip()
    )
    return Extraction(
        enabled=bool(raw.get("enabled", False)),
        prompt=str(raw.get("prompt", "")),
        variables=variables,
    )


def _node_from_raw(raw: dict[str, Any]) -> Node:
    data = raw.get("data") or {}
    return Node(
        id=str(raw.get("id", "")),
        type=str(raw.get("type", "")),
        name=str(data.get("name", "")).strip(),
        prompt=str(data.get("prompt", "")),
        greeting=data.get("greeting"),
        delayed_start_ms=int(data.get("delayed_start_ms") or 0),
        tools=[str(t) for t in (data.get("tools") or [])],
        knowledge_base_ids=[str(k) for k in (data.get("knowledge_base_ids") or [])],
        extraction=_parse_extraction(data.get("extraction")),
        transfer_destination=data.get("transfer_destination") or None,
        disposition=data.get("disposition") or None,
    )


def _edge_from_raw(raw: dict[str, Any]) -> Edge:
    data = raw.get("data") or {}
    speech = (data.get("transition_speech") or "").strip() or None
    return Edge(
        id=str(raw.get("id", "")),
        source=str(raw.get("source", "")),
        target=str(raw.get("target", "")),
        label=str(data.get("label", "")).strip(),
        condition=str(data.get("condition", "")).strip(),
        transition_speech=speech,
    )


def parse_graph(raw: dict[str, Any]) -> WorkflowGraph:
    """Raises WorkflowInvalid(errors) — never returns a partial graph."""
    errors = _structural_errors(raw)
    if errors:
        raise WorkflowInvalid(errors)

    nodes = {n.id: n for n in (_node_from_raw(r) for r in raw.get("nodes") or [])}
    for raw_edge in raw.get("edges") or []:
        edge = _edge_from_raw(raw_edge)
        nodes[edge.source].out_edges.append(edge)

    start_id = next(n.id for n in nodes.values() if n.type == "start")
    return WorkflowGraph(nodes=nodes, start_node_id=start_id)


def _structural_errors(raw: Any) -> list[WorkflowError]:
    """Every rule here is a runtime break, not a style preference — see the
    validation table in docs/workflow.md §5.1."""
    errors: list[WorkflowError] = []

    def err(kind: str, id_: str | None, field_: str | None, message: str) -> None:
        errors.append(WorkflowError(kind=kind, id=id_, field=field_, message=message))

    if not isinstance(raw, dict):
        return [WorkflowError("workflow", None, None, "workflow must be an object")]

    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        return [WorkflowError("workflow", None, "nodes", "workflow has no nodes")]
    raw_edges = raw.get("edges") or []
    if not isinstance(raw_edges, list):
        return [WorkflowError("workflow", None, "edges", "edges must be a list")]

    nodes: dict[str, Node] = {}
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            err("workflow", None, "nodes", "every node must be an object")
            continue
        node = _node_from_raw(raw_node)
        if not node.id:
            err("node", None, "id", "A step is missing its id — try reloading the page")
            continue
        if node.id in nodes:
            err("node", node.id, "id", f"Two steps share the id {node.id!r} — try reloading the page")
            continue
        if node.type not in NODE_TYPES:
            err("node", node.id, "type", f"{node.type!r} isn't a kind of step this editor knows about")
        if not node.name:
            err("node", node.id, "name", "This step needs a name — it's what you'll see in your call logs")
        nodes[node.id] = node
    if errors:
        return errors

    # Names appear in logs, transcripts and analytics — duplicates make
    # every "which stage did this call die in" answer a lie.
    seen_names: dict[str, str] = {}
    for node in nodes.values():
        clash = seen_names.get(node.name.lower())
        if clash is not None:
            err("node", node.id, "name",
                f"Another step is already called {node.name!r}. Names show up in your call "
                "logs and transcripts, so each one has to be different")
        seen_names[node.name.lower()] = node.id

    starts = [n for n in nodes.values() if n.type == "start"]
    if len(starts) != 1:
        err("workflow", None, None,
            "A flow needs exactly one starting point — "
            + ("there isn't one" if not starts else f"there are {len(starts)}"))
    if not any(n.type == "end" for n in nodes.values()):
        err("workflow", None, None,
            "Add a step that ends the call, otherwise the conversation has no way to finish")
    # Two of them would silently concatenate in whatever order the nodes
    # happen to be stored, which is a prompt nobody wrote.
    globals_ = [n for n in nodes.values() if n.is_unwired]
    if len(globals_) > 1:
        err("workflow", None, None,
            f"There are {len(globals_)} always-on instructions — keep one, and put anything "
            "step-specific on the step itself")

    edges: list[Edge] = []
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            err("workflow", None, "edges", "every edge must be an object")
            continue
        edge = _edge_from_raw(raw_edge)
        if edge.source not in nodes:
            err("edge", edge.id, "source", f"edge source {edge.source!r} is not a node in this workflow")
            continue
        if edge.target not in nodes:
            err("edge", edge.id, "target", f"edge target {edge.target!r} is not a node in this workflow")
            continue
        # A brand-new connection is missing both at once, and reporting it
        # twice for one thing the operator hasn't got to yet is just noise.
        if not edge.label and not edge.condition:
            err("edge", edge.id, "condition",
                "This connection isn't set up yet — it needs a short name and a description of "
                "when the call should take it")
        else:
            if not edge.label:
                err("edge", edge.id, "label",
                    "This connection needs a short name — it's how the agent refers to this move")
            elif not edge.tool_name:
                err("edge", edge.id, "label",
                    f"{edge.label!r} has no letters or numbers in it, so it can't be used as a name")
            if not edge.condition:
                err("edge", edge.id, "condition",
                    "This connection needs a condition. Without one the agent never knows when to "
                    "take it, so the call can't move on")
        edges.append(edge)

    out_edges: dict[str, list[Edge]] = {node_id: [] for node_id in nodes}
    in_count: dict[str, int] = {node_id: 0 for node_id in nodes}
    for edge in edges:
        out_edges[edge.source].append(edge)
        in_count[edge.target] += 1

    for node in nodes.values():
        if node.is_unwired:
            # Nothing leads in or out of a global node by design. React Flow
            # gives it no handles, so an edge touching one can only come from
            # a hand-written graph.
            if in_count[node.id] or out_edges[node.id]:
                err("node", node.id, None,
                    f"{node.name!r} applies to every step, so it isn't part of the flow — "
                    "remove the connection")
            continue
        if node.type == "start" and in_count[node.id]:
            err("node", node.id, None,
                "Nothing can lead back into the starting point — it's where every call begins")
        if node.is_terminal and out_edges[node.id]:
            err("node", node.id, None,
                f"{node.name!r} " + ("hands the call to a human" if node.type == "transfer" else "ends the call")
                + ", so nothing can lead out of it — remove the connection leaving it")
        if not node.is_terminal and not out_edges[node.id]:
            err("node", node.id, None,
                f"{node.name!r} has no way out. A call that reaches it would be stuck there "
                "until it hits the time limit — connect it to another step, or make it end the call")
        # "yes" and "Yes!" both become yes; whichever schema is registered
        # second silently shadows the first. This is the rule that actually
        # fires in practice.
        by_tool_name: dict[str, Edge] = {}
        for edge in out_edges[node.id]:
            if not edge.tool_name:
                continue
            clash = by_tool_name.get(edge.tool_name)
            if clash is not None:
                err("edge", edge.id, "label",
                    f"{edge.label!r} and {clash.label!r} look like the same move to the agent, so "
                    "only one of them would ever fire — give one a different name")
            by_tool_name[edge.tool_name] = edge

    return errors


def graph_warnings(graph: WorkflowGraph, known_variables: Iterable[str] = ()) -> list[WorkflowError]:
    """Editor mistakes that are not runtime breaks: a node nothing routes
    to, a {{ variable }} no node ever produces. Surfaced, never blocking —
    a half-built graph is a normal thing to publish once the reachable part
    of it is correct."""
    warnings: list[WorkflowError] = []
    reachable = graph.reachable()
    for node in graph.nodes.values():
        if node.is_unwired:
            continue          # never reachable, and that is the point
        if node.id not in reachable:
            warnings.append(WorkflowError(
                "node", node.id, None,
                f"No path leads to {node.name!r}, so no call will ever reach it",
            ))
    resolvable = (
        set(CALL_CONTEXT_VARIABLES) | graph.declared_variables() | set(known_variables)
    )
    for name in sorted(graph.template_variables() - resolvable):
        warnings.append(WorkflowError(
            "workflow", None, None,
            f"Nothing in this flow provides {{{{ {name} }}}}, so it will come out blank when the "
            "agent speaks. Check the spelling, or have a step capture it",
        ))
    return warnings


# ── The starter graph ────────────────────────────────────────────────────

def starter_graph(
    greeting: str = "", system_prompt: str = "", tools: list[str] | None = None,
) -> dict[str, Any]:
    """The graph every agent is born with (see services/config/agents.py's
    create_agent). Three nodes: the always-on instruction, the step the call
    starts in, and a way to finish. Not two — the validator rejects a
    non-terminal step with no way out, so start alone could never publish.

    `tools` goes on the start node — the only non-terminal step here, so the
    only one that can hold any. A brand-new agent has no enabled tools yet
    and correctly gets none; migrate_workflow_text.py passes the agent's
    existing ones, because Node.tools is default-deny and an agent that
    silently lost every tool the moment it gained a graph would be a
    behaviour change nobody asked for.

    Returned as the same raw React Flow dict the editor round-trips, not a
    WorkflowGraph — every consumer stores it as JSON, and admin-ui's own
    STARTER (lib/workflowApi.ts) mirrors it node for node so a graph drawn
    by the editor and one created by the server are the same shape.
    """
    return {
        "version": 1,
        "nodes": [
            {
                "id": "global", "type": "global", "position": {"x": 330, "y": 0},
                "data": {"name": "always applies", "prompt": system_prompt},
            },
            {
                "id": "start", "type": "start", "position": {"x": 0, "y": 0},
                "data": {
                    "name": "greeting",
                    "prompt": "Greet the caller and find out what they need.",
                    "greeting": greeting,
                    "tools": list(tools or []),
                },
            },
            {
                "id": "end", "type": "end", "position": {"x": 0, "y": 230},
                "data": {
                    "name": "goodbye",
                    "prompt": "Confirm anything outstanding and close warmly.",
                    "disposition": "completed",
                },
            },
        ],
        "edges": [
            {
                "id": "e-start-end", "source": "start", "target": "end",
                "data": {
                    "label": "conversation finished",
                    "condition": "The caller has no further questions.",
                },
            },
        ],
    }
