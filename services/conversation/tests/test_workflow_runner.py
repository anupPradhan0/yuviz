"""
Dry-run tests — the whole point of keeping WorkflowRunner free of voice
concerns (docs/workflow.md §7.2). A scripted walk through a graph, in
milliseconds, with no pipeline, no audio and no providers anywhere near it.

If a change to WorkflowRunner makes these tests need a pipeline, that
change took something away.
"""

from __future__ import annotations

import asyncio

from libs.config_sdk.workflow import parse_graph

from services.conversation.providers.interfaces import ChatMessage
from services.conversation.workflow import WorkflowRunner

GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "g1", "type": "global", "data": {
            "name": "always applies", "prompt": "You are Ada, a receptionist.",
        }},
        {"id": "n1", "type": "start", "data": {
            "name": "greeting",
            "prompt": "Greet the caller and find out what they want.",
            "greeting": "Hi, thanks for calling {{ business_name | the clinic }}.",
        }},
        {"id": "n2", "type": "agent", "data": {
            "name": "booking",
            "prompt": "Book an appointment for {{ caller_number }}.",
            "tools": ["book_appointment"],
            "knowledge_base_ids": [],
            "extraction": {"enabled": True, "prompt": "Only what they said.",
                           "variables": [{"name": "reason", "type": "string",
                                          "prompt": "Why they want the appointment."}]},
        }},
        {"id": "n3", "type": "agent", "data": {
            "name": "qanda", "prompt": "Answer their question.",
            "knowledge_base_ids": ["kb1"],
        }},
        {"id": "n4", "type": "transfer", "data": {
            "name": "to_human", "prompt": "Say you're connecting them.",
            "transfer_destination": "+15559999",
        }},
        {"id": "n5", "type": "end", "data": {
            "name": "goodbye", "prompt": "Close warmly.", "disposition": "qualified",
        }},
    ],
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2", "data": {
            "label": "caller wants to book",
            "condition": "The caller has asked to make an appointment.",
            "transition_speech": "Of course, let me pull up the calendar.",
        }},
        {"id": "e2", "source": "n1", "target": "n3", "data": {
            "label": "just a question", "condition": "The caller asked a question."}},
        {"id": "e3", "source": "n1", "target": "n4", "data": {
            "label": "wants a human", "condition": "The caller asked for a person."}},
        {"id": "e4", "source": "n2", "target": "n5", "data": {
            "label": "booked", "condition": "The appointment is booked."}},
        {"id": "e5", "source": "n3", "target": "n5", "data": {
            "label": "answered", "condition": "Their question is answered."}},
    ],
}


def _runner(**kwargs) -> WorkflowRunner:
    # No global_prompt argument: the always-on instruction is the graph's
    # own global node (g1 above), not something handed in beside it.
    kwargs.setdefault("base_suffix", "Today is 2026-08-28.")
    kwargs.setdefault("variables", {"caller_number": "+15551234"})
    return WorkflowRunner(parse_graph(GRAPH), **kwargs)


def test_qualified_path():
    runner = _runner()
    assert runner.node.name == "greeting"

    tools = runner.local_tools()
    assert set(tools) == {"caller_wants_to_book", "just_a_question", "wants_a_human"}
    asyncio.run(tools["caller_wants_to_book"][1]({}))

    assert runner.node.name == "booking"
    assert runner.allowed_tool_names() == ["book_appointment"]
    assert runner.pending_speech == "Of course, let me pull up the calendar."

    asyncio.run(runner.local_tools()["booked"][1]({}))
    assert runner.node.name == "goodbye"
    assert runner.pending_end is True
    assert runner.disposition == "qualified"
    assert runner.visited == ["greeting", "booking", "goodbye"]


def test_the_booking_tool_does_not_exist_until_the_booking_node_is_active():
    # Not "the model is told not to use it" — it is genuinely not in the
    # tool list sent to the provider on turn one.
    runner = _runner()
    assert runner.allowed_tool_names() == []


def test_prompt_is_global_then_node_then_suffix_and_renders_variables():
    runner = _runner()
    asyncio.run(runner.local_tools()["caller_wants_to_book"][1]({}))
    assert runner.system_prompt() == (
        "You are Ada, a receptionist.\n\n"
        "Book an appointment for +15551234.\n\n"
        "Today is 2026-08-28."
    )


def test_greeting_comes_from_the_start_node_with_a_fallback():
    assert _runner().greeting() == "Hi, thanks for calling the clinic."
    runner = _runner(variables={"business_name": "Oak Dental"})
    assert runner.greeting() == "Hi, thanks for calling Oak Dental."


def test_extracted_variables_reach_later_nodes_prompts():
    runner = _runner()
    runner.update_variables({"caller_number": "+15550000"})
    asyncio.run(runner.local_tools()["caller_wants_to_book"][1]({}))
    assert "+15550000" in runner.system_prompt()


def test_transition_swaps_the_system_prompt_inside_the_same_turn():
    # The trap in §5.3: run_turn mutates history in place, so a transition
    # that only took effect between turns would leave the rest of this
    # turn generating under the previous node's prompt.
    runner = _runner()
    history = [
        ChatMessage(role="system", content=runner.system_prompt()),
        ChatMessage(role="user", content="I'd like to book something"),
    ]
    asyncio.run(runner.local_tools(history)["caller_wants_to_book"][1]({}))
    assert "Book an appointment" in history[0].content
    assert history[1].role == "user"


def test_reaching_a_transfer_node_flags_a_transfer_not_an_end():
    runner = _runner()
    asyncio.run(runner.local_tools()["wants_a_human"][1]({}))
    assert runner.pending_end is False
    assert runner.pending_transfer is not None
    assert runner.pending_transfer.transfer_destination == "+15559999"


def test_knowledge_is_per_stage():
    runner = _runner()
    assert runner.knowledge_enabled() is False       # start node has no KB
    asyncio.run(runner.local_tools()["just_a_question"][1]({}))
    assert runner.knowledge_enabled() is True        # q&a node does


def test_extraction_fires_before_leaving_the_node_not_after():
    seen: list[tuple[str, int]] = []

    class _Extractor:
        def extract(self, node, history):
            seen.append((node.name, len(history)))

    runner = _runner(extractor=_Extractor())
    asyncio.run(runner.local_tools()["caller_wants_to_book"][1]({}))
    assert seen == []                       # the start node declares none
    asyncio.run(runner.local_tools()["booked"][1]({}))
    # Extracted from the booking node, while booking was still the active
    # one — after the swap this segment is just historical context.
    assert seen == [("booking", 0)]


def test_no_dead_ends_and_the_happy_path_reaches_an_end_node():
    # The check every published graph should carry (§7.2).
    graph = parse_graph(GRAPH)
    for node in graph.nodes.values():
        # A global node is wired to nothing on purpose — it applies to every
        # step rather than being one.
        if node.is_unwired:
            continue
        assert node.is_terminal or node.out_edges, f"{node.name} is a dead end"
    assert any(graph.nodes[n].type == "end" for n in graph.reachable())


def test_a_graph_with_no_global_node_just_has_no_global_prefix():
    """Every instruction being per-step is a legitimate graph, not an error —
    the composition simply starts at the node's own prompt."""
    bare = {**GRAPH, "nodes": [n for n in GRAPH["nodes"] if n["type"] != "global"]}
    runner = WorkflowRunner(parse_graph(bare), base_suffix="Today is 2026-08-28.")
    assert runner.system_prompt() == (
        "Greet the caller and find out what they want.\n\nToday is 2026-08-28."
    )
