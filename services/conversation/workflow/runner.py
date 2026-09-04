"""
WorkflowRunner — the node walk (see docs/workflow.md §5.2).

Owns exactly one thing: which node is active. Its entire public surface is
"what prompt, what tools, what happened" — no audio, no frames, no provider
objects, nothing that knows a phone call exists. That constraint is what
makes the dry-run tests in §7.2 possible (a scripted list of caller turns
walks a graph in milliseconds, with no pipeline anywhere near it), so it is
worth defending in review rather than quietly relaxing.

The mechanism is one idea: each outgoing edge of the active node is
registered with the LLM as a callable function, named after the edge label
and described by the edge condition. The model advances the conversation by
calling it. Decision and action are the same event — there is no window
where the model believes it has advanced but the engine hasn't, and every
transition lands in the logs as a named function call.

Constructed per call (PipelineConversationHandler already is — see
servicer.py's handler_factory), so the current-node pointer is a plain
instance attribute: no session map, no cross-call leakage, no cleanup path.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Awaitable, Callable

from libs.config_sdk import RuntimeConfig
from libs.config_sdk.workflow import (
    ENDED_EARLY, Edge, Node, WorkflowGraph, WorkflowInvalid, parse_graph, render,
    starter_graph,
)

from ..providers.interfaces import ChatMessage
from ..tools.types import ToolDefinition, ToolResult, ToolStatus

log = logging.getLogger(__name__)

# A transition takes no arguments — the decision itself is the entire
# payload. Kept as an explicit empty object rather than omitted: every
# IToolAwareLLM implementation passes `parameters` straight through to its
# provider (see llm_adapter.py's to_generic_schema), and a missing key is
# the shape most likely to differ between them.
_NO_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}}

# What a transition returns to the model. Deliberately minimal and always
# identical: the model does not need to be told anything about the node it
# just entered — the next generation runs under that node's own prompt,
# which says it far better than a tool payload could. The extractor strips
# these from its transcript for exactly that reason (see
# extractor.py) — dozens of them accumulate and they are noise.
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
        # The always-on instruction, from the graph's own global node — not
        # from a column beside it. One place to look when an agent misbehaves
        # (docs/workflow.md §9.1).
        self._global = graph.global_prompt
        self._suffix = base_suffix            # current date + [[END_CALL]] marker instruction
        self._vars: dict[str, Any] = dict(variables or {})
        self._extractor = extractor
        self._summarizer = summarizer
        self.visited: list[str] = [self._node.name]
        # Read and cleared by the pipeline after each turn (see §5.4). Flags
        # rather than actions: this class does not speak, hang up, or
        # transfer — it only reports that one of those is now due.
        self.pending_speech: str | None = None
        self.pending_end: bool = False
        self.pending_transfer: Node | None = None
        # Set by the pipeline when the call ended on [[END_CALL]] from a
        # non-terminal node — see `disposition` below.
        self.ended_off_graph: bool = False
        # The edge that produced the current node — reported alongside the
        # transition so a log line and the editor both say WHY it moved, not
        # just where to.
        self.last_transition: str = ""

    # ── What the pipeline asks for each turn ──────────────────────────────

    @property
    def node(self) -> Node:
        return self._node

    @property
    def variables(self) -> dict[str, Any]:
        return dict(self._vars)

    def update_variables(self, values: dict[str, Any]) -> None:
        """Extraction results land here (see extractor.py), so a
        later node's prompt can say {{ policy_number }} about something the
        caller said three nodes ago."""
        self._vars.update({k: v for k, v in values.items() if v is not None})

    def system_prompt(self) -> str:
        """Composed fresh every turn, not cached: rendering happens at
        compose time so variables extracted earlier in the call appear in
        later nodes' prompts."""
        parts = [
            self.render(self._global),
            self.render(self._node.prompt),
            self._suffix,
        ]
        return "\n\n".join(p.strip() for p in parts if p and p.strip())

    def allowed_tool_names(self) -> list[str]:
        """The node's own tool list, passed to ToolPolicyResolver as a
        narrowing filter. An empty list means "no tools this stage", not
        "all tools" — withholding capability until it's earned is the point
        of stages (see policy_resolver.py's `only`).

        Default-deny is why migrate_workflow_text.py has to write each
        agent's currently-enabled tools onto the start node it creates:
        without that, migrating an agent silently took away every tool it
        had, which is a behaviour change no operator asked for."""
        return list(self._node.tools)

    def knowledge_enabled(self) -> bool:
        """Whether this stage does RAG at all. Per-KB selection is stored on
        the node and shown in the editor, but only the on/off half is
        enforced here.
        ponytail: filtering to specific knowledge_base_ids needs a
        knowledge_base_ids field on knowledge_sdk's RetrievalPolicy and a
        matching filter in services/knowledge/retrieval.py — add both when
        an agent actually has two KBs that must not mix."""
        return bool(self._node.knowledge_base_ids)

    def greeting(self) -> str | None:
        """The start node's greeting wins over the agent's own — an
        operator editing the graph should not have to remember that the
        first thing the caller hears lives on a different tab."""
        text = self.render(self._graph.start.greeting or "")
        return text or None

    @property
    def delayed_start_ms(self) -> int:
        return self._graph.start.delayed_start_ms

    @property
    def disposition(self) -> str | None:
        """The end node's code — or ENDED_EARLY when the model hung up with
        [[END_CALL]] somewhere in the middle of the graph, which reports no
        code of its own. Recording nothing there is indistinguishable from a
        caller who just hung up, and the two want opposite fixes: one is a
        missing edge in the graph, the other is a caller."""
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
        """One in-process tool per outgoing edge, in the shape
        ToolCallOrchestrator's `local_tools` takes.

        `turn` is the message list this turn is generating from, handed in
        rather than held as state: the transition has to swap turn[0]
        *inside* the turn (see the trap in docs/workflow.md §5.3 — run_turn
        mutates its list in place, so a transition that only took effect
        between turns would run the rest of this turn's generations under
        the previous node's prompt with the new node's tools, which mostly
        works, which is what makes it nasty).

        `store` is the session's persistent history, which on a
        knowledge-enabled node is a *different* list: the RAG branch builds
        `history[:-1] + [augmented]` so the retrieved context rides this
        turn only. The two need separating because the prompt swap has to
        reach both (the copy for the rest of this turn, the real list for
        the next one) while summarization and extraction must only ever see
        the real one — splicing the copy threw the work away, and feeding
        extraction the injected RAG block would have it extract from text
        the caller never said. Defaults to `turn`, which is the same object
        whenever retrieval didn't run.

        Both are omitted entirely by the dry-run tests, which have no
        history to swap.
        """
        tools: dict[str, tuple[ToolDefinition, Callable[..., Awaitable[ToolResult]]]] = {}
        for edge in self._node.out_edges:
            definition = ToolDefinition(
                name=edge.tool_name,
                # The condition is the prompt that actually decides the
                # transition — it matters more than the node prompt, which
                # is why the editor gives it more room than the label.
                description=edge.condition,
                parameters_schema=_NO_PARAMETERS,
                category="workflow_transition",
            )

            def handler(_args: dict[str, Any], _edge: Edge = edge) -> Awaitable[ToolResult]:
                return self._transition(_edge, turn, store if store is not None else turn)

            tools[edge.tool_name] = (definition, handler)
        return tools

    # ── The state machine ─────────────────────────────────────────────────

    async def _transition(
        self,
        edge: Edge,
        turn: list[ChatMessage] | None,
        store: list[ChatMessage] | None,
    ) -> ToolResult:
        source = self._node

        # 1. Extract before leaving. This node's slice of the conversation
        #    is the extraction window; after the swap it is just historical
        #    context the next node's extraction would have to re-derive.
        if self._extractor is not None and source.extraction is not None and source.extraction.enabled:
            self._extractor.extract(source, store or [])

        # 2. Queue the bridging line, spoken before the next generation so
        #    the transition's round-trip isn't dead air.
        self.pending_speech = self.render(edge.transition_speech or "") or None

        # 3. Move.
        self._node = self._graph.nodes[edge.target]
        self.last_transition = edge.tool_name
        self.visited.append(self._node.name)
        log.info(
            "workflow: %s --%s--> %s", source.name, edge.tool_name, self._node.name,
        )

        # 4. Flag terminals for the pipeline to act on after the turn. Both
        #    reuse the existing end-call/transfer paths; neither invents a
        #    new teardown.
        if self._node.type == "end":
            self.pending_end = True
        elif self._node.type == "transfer":
            self.pending_transfer = self._node

        # 5. Swap the prompt for the remainder of THIS turn — see
        #    local_tools()'s docstring for why between-turns is too late,
        #    and why it has to land on both lists when they differ.
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
                break            # same object, don't swap it twice

        # Only ever the persistent list: a summary spliced into this turn's
        # throwaway RAG copy is discarded the moment the turn ends.
        if store and self._summarizer is not None:
            self._summarizer.maybe_summarize(store)

        return _TRANSITION_RESULT


# ── Parsing, once per (agent, config_version) ────────────────────────────
# Parsing per call is wasted work on the latency path, and a parse failure
# discovered at call time is a dropped call. Warmed at startup by
# _prewarm_agents (see __main__.py), where the same failure is a log line
# and a fallback to the starter graph. Keyed by config_version, so a
# publish (which bumps it) invalidates this without any extra plumbing;
# unbounded only in the sense that agents * publishes is unbounded, which
# is a handful of small dicts on a long-lived process.
_GRAPH_CACHE: dict[tuple[str, int, bool], WorkflowGraph] = {}


def graph_for(runtime_config: RuntimeConfig, *, draft: bool = False) -> WorkflowGraph:
    """Every agent runs a graph — an agent IS its workflow (docs/workflow.md
    §9.1), and one is created with the row itself. There is no single-prompt
    mode to fall back to any more.

    Never raises, and never returns None: a config-plane problem must not
    reject a call (same posture as agent_resolver.py's fallback). When the
    stored graph is missing or unparseable — which a published graph can only
    be after a code regression, since publish validates — the call runs the
    built-in starter graph. Degrading to a generic-but-working agent beats
    dropping a caller, and the log line says loudly which agent to republish.

    draft=True runs the agent's UNPUBLISHED graph instead — only ever set
    by the admin UI's own test-call path (see SessionOpenRequest.
    use_workflow_draft), so an operator can hear a change before publishing
    it to real traffic. A draft is not validated on save, so this is the
    one place a graph that fails to parse is expected rather than alarming;
    it falls back to the published one.
    """
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
        return _fallback_graph()
    # Drafts are deliberately NOT cached: a draft save doesn't bump
    # config_version (see bump_agent_config_version in database/schema.sql),
    # so there is no key that would go stale correctly — a cached draft would
    # make every later test call replay the first one the process ever saw.
    # Test calls are rare and off the latency path, so parsing each time is
    # the cheaper mistake.
    key = (runtime_config.agent.id or runtime_config.agent.slug, runtime_config.version, draft)
    if not draft and key in _GRAPH_CACHE:
        return _GRAPH_CACHE[key]
    try:
        graph = parse_graph(raw)
    except WorkflowInvalid as exc:
        if draft:
            # A draft is never validated on save, so a half-drawn one is
            # expected here rather than alarming — test the published graph
            # instead of the starter one mid-test.
            log.info(
                "workflow: draft for agent %s does not parse (%s) — testing the "
                "published graph instead", runtime_config.agent.slug, exc,
            )
            return graph_for(runtime_config, draft=False)
        log.error(
            "workflow: agent %s has a published graph that does not parse (%s) — "
            "running the starter graph; republish it from the editor",
            runtime_config.agent.slug, exc,
        )
        graph = _fallback_graph()
    except Exception:
        log.exception("workflow: unexpected failure parsing graph for agent %s", runtime_config.agent.slug)
        graph = _fallback_graph()
    if not draft:
        _GRAPH_CACHE[key] = graph
    return graph


@lru_cache(maxsize=1)
def _fallback_graph() -> WorkflowGraph:
    """Parsed once per process — it is the same three nodes every time, and
    the only paths that reach it are already error paths."""
    return parse_graph(starter_graph())
