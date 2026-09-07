"""
One end-to-end pass through the real pipeline: handler + orchestrator,
scripted tool-aware LLM. Covers mid-turn prompt swap, tool scoping, end node,
transition speech, and workflow transfer.
"""

from __future__ import annotations

from services.conversation.tools.executor_registry import ExecutorRegistry
from services.conversation.tools.llm_adapter import LLMAdapter, TokenEvent, ToolCallEvent
from services.conversation.tools.orchestrator import ToolCallOrchestrator

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

TRANSFER_GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "g1", "type": "global", "data": {
            "name": "always applies", "prompt": "You are Ada."}},
        {"id": "n1", "type": "start", "data": {
            "name": "greeting", "prompt": "Ask what they need.",
            "greeting": "Thanks for calling."}},
        {"id": "n2", "type": "transfer", "data": {
            "name": "to_human", "prompt": "Say you're connecting them.",
            "transfer_destination": "+15559999"}},
        {"id": "n3", "type": "end", "data": {
            "name": "goodbye", "prompt": "Close.", "disposition": "completed"}},
    ],
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2", "data": {
            "label": "wants a human", "condition": "The caller asked for a person."}},
        {"id": "e2", "source": "n1", "target": "n3", "data": {
            "label": "all done", "condition": "The caller is finished."}},
    ],
}


class _ScriptedToolLLM:
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

    async def generate_with_tools(self, messages, schemas, tool_choice=None):
        self.seen_prompts.append(messages[0].content if messages else "")
        self.seen_tool_names.append([s["name"] for s in schemas])
        for event in self._generations.pop(0):
            yield event


class _RecordingPolicyResolver:
    def __init__(self):
        self.seen_only: list[list[str] | None] = []

    async def enabled_tools(self, agent_id, only=None):
        self.seen_only.append(only)
        return []


def _handler(llm, resolver, *, workflow=GRAPH, **kw):
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=resolver,
        provider_manager=None,
        executor_registry=ExecutorRegistry(),
    )
    return _make_handler(
        _make_stt("I'd like to book an appointment"), llm, _make_tts(),
        system_prompt="You are Ada.", workflow=workflow, tool_orchestrator=orchestrator,
        **kw,
    )


async def test_a_call_walks_the_graph_and_ends_on_the_end_node():
    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="wants_to_book", arguments={})],
        [TokenEvent(text="Sure."), TokenEvent(text=" What time suits you?")],
        [ToolCallEvent(tool_call_id="t2", tool_name="booked", arguments={})],
        [TokenEvent(text="You're all set. Goodbye!")],
    ])
    resolver = _RecordingPolicyResolver()
    handler = _handler(llm, resolver)

    assert await handler.greeting("s1") != []

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

    assert llm.seen_tool_names[0] == ["wants_to_book"]
    assert "Ask what they need." in llm.seen_prompts[0]
    assert "Take their preferred time." in llm.seen_prompts[1]
    assert llm.seen_prompts[1].startswith("You are Ada.")
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
    assert spoken.index("Let me pull up the calendar.") < spoken.index("Sure.")
    assert handler._workflow.pending_speech is None


async def test_an_end_call_marker_survives_a_transition_in_the_same_turn():
    """[[END_CALL]] must still hang up if transition speech yields in the same turn."""
    llm = _ScriptedToolLLM([
        [
            TokenEvent(text="All set. [[END_CALL]]"),
            ToolCallEvent(tool_call_id="t1", tool_name="wants_to_book", arguments={}),
        ],
        [],
    ])
    handler = _handler(llm, _RecordingPolicyResolver())

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]

    assert any(r.end_call for r in responses), (
        "the hang-up was lost — transition speech reset end_call"
    )


async def test_workflow_transfer_node_surfaces_a_transfer_request():
    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="wants_a_human", arguments={})],
        [TokenEvent(text="Connecting you now.")],
    ])
    handler = _handler(
        llm, _RecordingPolicyResolver(), workflow=TRANSFER_GRAPH,
        transfer_type="warm", transfer_destination="+15550001111",
    )

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]

    assert handler._workflow.node.name == "to_human"
    transfers = [r.transfer_request for r in responses if r.transfer_request]
    assert len(transfers) == 1
    assert transfers[0].destination == "+15559999"
    assert transfers[0].trigger == "workflow_policy"
    assert not any(r.end_call for r in responses)


async def test_workflow_transfer_rejected_when_agent_transfer_disabled():
    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="wants_a_human", arguments={})],
        [TokenEvent(text="One moment.")],
    ])
    handler = _handler(
        llm, _RecordingPolicyResolver(), workflow=TRANSFER_GRAPH,
        transfer_type="none",
    )

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]

    assert handler._workflow.node.name == "to_human"
    assert not any(r.transfer_request for r in responses)
