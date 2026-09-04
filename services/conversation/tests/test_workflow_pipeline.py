"""
One end-to-end pass through the real pipeline (docs/workflow.md §7.3): a
real PipelineConversationHandler and a real ToolCallOrchestrator, driven by
a scripted tool-aware LLM that emits transition calls. Asserts the things
the dry-run tests can't see — that the graph actually walks under a live
turn, that the prompt swaps mid-turn, that per-node tool scoping reaches
the resolver, and that an `end` node ends the call.
"""

from __future__ import annotations

import pytest

from services.conversation.tools.executor_registry import ExecutorRegistry
from services.conversation.tools.llm_adapter import LLMAdapter, TokenEvent, ToolCallEvent
from services.conversation.tools.orchestrator import ToolCallOrchestrator
from services.conversation.workflow.runner import _GRAPH_CACHE

from services.conversation.pipeline import _FALLBACK_GOODBYE

from .test_pipeline import _make_handler, _make_stt, _make_tts, _silence

GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "g1", "type": "global", "data": {
            "name": "always applies", "prompt": "You are Ada."}},
        {"id": "n1", "type": "start", "data": {
            "name": "greeting", "prompt": "Ask what they need.",
            "greeting": "Thanks for calling."}},
        {"id": "n2", "type": "agent", "data": {
            "name": "booking", "prompt": "Take their preferred time.",
            "tools": ["book_appointment"]}},
        {"id": "n3", "type": "end", "data": {
            "name": "goodbye", "prompt": "Say goodbye.", "disposition": "qualified"}},
    ],
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2", "data": {
            "label": "wants to book", "condition": "The caller asked to book.",
            "transition_speech": "Let me pull up the calendar."}},
        {"id": "e2", "source": "n2", "target": "n3", "data": {
            "label": "booked", "condition": "The appointment is booked."}},
    ],
}


class _ScriptedToolLLM:
    """Emits a pre-scripted list of events per generation, and records the
    system prompt and tool schemas it was handed each time."""

    def __init__(self, generations):
        self._generations = list(generations)
        self.seen_prompts: list[str] = []
        self.seen_tool_names: list[list[str]] = []

    async def generate(self, messages):
        self.seen_prompts.append(messages[0].content if messages else "")
        self.seen_tool_names.append([])
        for event in self._generations.pop(0):
            assert isinstance(event, TokenEvent)
            yield event.text

    async def generate_with_tools(self, messages, schemas):
        self.seen_prompts.append(messages[0].content if messages else "")
        self.seen_tool_names.append([s["name"] for s in schemas])
        for event in self._generations.pop(0):
            yield event


class _RecordingPolicyResolver:
    def __init__(self):
        self.seen_only: list[list[str] | None] = []

    async def enabled_tools(self, agent_id, only=None):
        self.seen_only.append(only)
        return []   # no tool_provider_configs in a unit test


@pytest.fixture(autouse=True)
def _clear_graph_cache():
    # graph_for() caches by (agent id, config_version), and every handler
    # these tests build reuses both.
    _GRAPH_CACHE.clear()
    yield
    _GRAPH_CACHE.clear()


def _handler(llm, resolver, **kw):
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=resolver,
        provider_manager=None,          # never reached: no policy tools resolve
        executor_registry=ExecutorRegistry(),
    )
    return _make_handler(
        _make_stt("I'd like to book an appointment"), llm, _make_tts(),
        system_prompt="You are Ada.", workflow=GRAPH, tool_orchestrator=orchestrator,
        **kw,
    )


async def test_a_call_walks_the_graph_and_ends_on_the_end_node():
    llm = _ScriptedToolLLM([
        # Turn 1: the model advances the conversation by calling the edge.
        [ToolCallEvent(tool_call_id="t1", tool_name="wants_to_book", arguments={})],
        [TokenEvent(text="Sure."), TokenEvent(text=" What time suits you?")],
        # Turn 2: booked -> the end node.
        [ToolCallEvent(tool_call_id="t2", tool_name="booked", arguments={})],
        [TokenEvent(text="You're all set. Goodbye!")],
    ])
    resolver = _RecordingPolicyResolver()
    handler = _handler(llm, resolver)

    assert await handler.greeting("s1") != []      # start node's greeting

    turn1 = [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]
    assert handler._workflow.node.name == "booking"
    assert any(r.tts_payloads for r in turn1)
    assert not any(r.end_call for r in turn1)

    turn2 = [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]
    assert handler._workflow.node.name == "goodbye"
    assert any(r.end_call for r in turn2)
    assert handler._workflow.visited == ["greeting", "booking", "goodbye"]
    assert handler._workflow.disposition == "qualified"


async def test_transitions_are_offered_as_tools_and_the_prompt_swaps_mid_turn():
    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="wants_to_book", arguments={})],
        [TokenEvent(text="Sure.")],
    ])
    resolver = _RecordingPolicyResolver()
    handler = _handler(llm, resolver)

    [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]

    # The start node's one outgoing edge was the only tool offered.
    assert llm.seen_tool_names[0] == ["wants_to_book"]
    # First generation ran under the start node's prompt...
    assert "Ask what they need." in llm.seen_prompts[0]
    # ...and the SECOND generation of the same turn ran under the new
    # node's, not the old one's. Getting this wrong mostly works, which is
    # what makes it nasty (see docs/workflow.md §5.3).
    assert "Take their preferred time." in llm.seen_prompts[1]
    # The graph's global node is the prefix on both.
    assert llm.seen_prompts[1].startswith("You are Ada.")

    # And the tool set moved with the node, not just the prompt: the start
    # node allows nothing, the booking node allows book_appointment. Offering
    # the OLD node's tools for the rest of the turn would both withhold the
    # tool the new prompt just told the model to use and leave the edges it
    # already left callable.
    assert resolver.seen_only == [[], ["book_appointment"]]
    assert llm.seen_tool_names[1] == ["booked"]


async def test_transition_speech_is_spoken_during_the_round_trip():
    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="wants_to_book", arguments={})],
        [TokenEvent(text="Sure.")],
    ])
    handler = _handler(llm, _RecordingPolicyResolver())
    tts = handler._tts

    [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]

    spoken = [call.args[0] for call in tts.synthesize.await_args_list]
    assert "Let me pull up the calendar." in spoken
    # Spoken before the new node's own words, not after them.
    assert spoken.index("Let me pull up the calendar.") < spoken.index("Sure.")
    assert handler._workflow.pending_speech is None



async def test_an_end_call_marker_survives_a_transition_in_the_same_turn():
    """[[END_CALL]] emitted alongside a transition must still hang up.

    on_speech_ended does `end_call = marker_seen` on EVERY item _llm_to_tts
    yields, so the transition-speech branch yielding a literal False after
    the marker had already been seen silently un-hung-up the call — and the
    follow-up generation producing no text left nothing to re-raise it.
    """
    llm = _ScriptedToolLLM([
        [
            TokenEvent(text="All set. [[END_CALL]]"),
            ToolCallEvent(tool_call_id="t1", tool_name="wants_to_book", arguments={}),
        ],
        [],                     # nothing further to say after the transition
    ])
    handler = _handler(llm, _RecordingPolicyResolver())

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]

    assert any(r.end_call for r in responses), (
        "the hang-up was lost — transition speech reset end_call"
    )


async def test_transition_speech_reaches_a_chat_session_as_text():
    """_synthesize_sentence_stream is a no-op in text mode, so the bridging
    line has to be yielded as words or it vanishes — and the chat panel is
    where a workflow gets tested before it goes near a phone."""
    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="wants_to_book", arguments={})],
        [TokenEvent(text="Sure.")],
    ])
    handler = _handler(llm, _RecordingPolicyResolver(), text_only=True)

    said = " ".join(
        r.agent_text for r in [r async for r in handler.on_text("s1", "book me in")]
        if r.agent_text
    )
    assert "Let me pull up the calendar." in said
