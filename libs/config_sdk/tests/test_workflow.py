"""
Graph model + validation. Pure functions, no infra — every rule enforced
here is a runtime break, so each one gets the case that would actually
happen in a call.
"""

from __future__ import annotations

import pytest

from libs.config_sdk.workflow import (
    WorkflowInvalid,
    graph_warnings,
    parse_graph,
    render,
    starter_graph,
)


def _node(id_, type_, name, **data):
    return {"id": id_, "type": type_, "data": {"name": name, **data}}


def _edge(id_, source, target, label, condition="something happened", **data):
    return {
        "id": id_, "source": source, "target": target,
        "data": {"label": label, "condition": condition, **data},
    }


def _graph(nodes=None, edges=None):
    return {
        "version": 1,
        "nodes": nodes if nodes is not None else [
            _node("n1", "start", "greeting", prompt="Say hello."),
            _node("n2", "agent", "booking", prompt="Book it.", tools=["book_appointment"]),
            _node("n3", "end", "goodbye", prompt="Close.", disposition="qualified"),
        ],
        "edges": edges if edges is not None else [
            _edge("e1", "n1", "n2", "caller wants to book",
                  transition_speech="Let me pull up the calendar."),
            _edge("e2", "n2", "n3", "booked"),
        ],
    }


def _errors(graph):
    with pytest.raises(WorkflowInvalid) as exc:
        parse_graph(graph)
    return exc.value.errors


def test_parses_a_valid_graph_into_nodes_and_out_edges():
    graph = parse_graph(_graph())
    assert graph.start.name == "greeting"
    assert [e.target for e in graph.start.out_edges] == ["n2"]
    assert graph.nodes["n2"].tools == ["book_appointment"]
    assert graph.nodes["n3"].disposition == "qualified"


def test_edge_label_becomes_a_callable_tool_name():
    graph = parse_graph(_graph())
    assert graph.start.out_edges[0].tool_name == "caller_wants_to_book"


def test_two_labels_collapsing_to_the_same_tool_name_is_rejected():
    # "yes" and "Yes!" both become `yes` — whichever schema is registered
    # second silently shadows the first, and the transition never fires.
    errors = _errors(_graph(edges=[
        _edge("e1", "n1", "n2", "yes"),
        _edge("e2", "n1", "n3", "Yes!"),
        _edge("e3", "n2", "n3", "booked"),
    ]))
    assert any("same move to the agent" in e.message for e in errors)


def test_a_non_terminal_node_with_no_outgoing_edge_is_rejected():
    errors = _errors(_graph(edges=[_edge("e1", "n1", "n3", "done")]))
    assert any(e.id == "n2" and "no way out" in e.message for e in errors)


def test_dangling_edge_target_is_rejected():
    errors = _errors(_graph(edges=[
        _edge("e1", "n1", "n2", "go"),
        _edge("e2", "n2", "nope", "booked"),
    ]))
    assert any(e.field == "target" for e in errors)


def test_terminal_nodes_may_not_have_outgoing_edges():
    errors = _errors(_graph(edges=[
        _edge("e1", "n1", "n2", "go"),
        _edge("e2", "n2", "n3", "booked"),
        _edge("e3", "n3", "n2", "again"),
    ]))
    assert any(e.id == "n3" and "nothing can lead out of it" in e.message for e in errors)


def test_start_node_may_not_have_incoming_edges():
    errors = _errors(_graph(edges=[
        _edge("e1", "n1", "n2", "go"),
        _edge("e2", "n2", "n1", "back"),
        _edge("e3", "n2", "n3", "booked"),
    ]))
    assert any(e.id == "n1" and "lead back into the starting point" in e.message for e in errors)


def test_exactly_one_start_and_at_least_one_end():
    assert any("exactly one starting point" in e.message for e in _errors(_graph(nodes=[
        _node("n1", "start", "a"), _node("n2", "start", "b"), _node("n3", "end", "c"),
    ], edges=[])))
    assert any("ends the call" in e.message for e in _errors(_graph(nodes=[
        _node("n1", "start", "a"), _node("n2", "agent", "b"),
    ], edges=[_edge("e1", "n1", "n2", "go")])))


def test_duplicate_node_names_are_rejected():
    # They appear in logs and transcripts — duplicates make analytics lie.
    errors = _errors(_graph(nodes=[
        _node("n1", "start", "same"), _node("n2", "agent", "same"), _node("n3", "end", "done"),
    ]))
    assert any(e.field == "name" for e in errors)


def test_an_edge_with_no_condition_is_rejected():
    errors = _errors(_graph(edges=[
        _edge("e1", "n1", "n2", "go", condition=""),
        _edge("e2", "n2", "n3", "booked"),
    ]))
    assert any(e.field == "condition" for e in errors)


def test_a_brand_new_connection_reports_one_problem_not_two():
    """It's missing both a name and a condition the instant it's drawn —
    saying so twice is noise about something the operator simply hasn't got
    to yet."""
    errors = _errors(_graph(edges=[
        _edge("e1", "n1", "n2", "", condition=""),
        _edge("e2", "n2", "n3", "booked"),
    ]))
    edge_errors = [e for e in errors if e.id == "e1"]
    assert len(edge_errors) == 1
    assert "isn't set up yet" in edge_errors[0].message


def test_error_messages_are_written_for_the_person_drawing_the_flow():
    """These strings are rendered verbatim in the editor's problem list, so
    they must not leak the vocabulary of the implementation."""
    jargon = ("node", "edge", "LLM", "tool name", "outgoing")
    errors = _errors(_graph(edges=[_edge("e1", "n1", "n3", "done")]))
    for e in errors:
        assert not any(word in e.message for word in jargon), e.message


def test_cycles_are_allowed():
    # "the caller has another question" looping back to Q&A is correct
    # behavior, not a bug — runaway loops are bounded by max_call_duration_s.
    graph = parse_graph(_graph(edges=[
        _edge("e1", "n1", "n2", "go"),
        _edge("e2", "n2", "n3", "booked"),
        _edge("e3", "n2", "n2", "another question"),
    ]))
    assert "n2" in graph.reachable()


def test_unreachable_node_is_a_warning_not_an_error():
    graph = parse_graph({
        "nodes": [
            _node("n1", "start", "greeting"),
            _node("n2", "agent", "orphan", prompt="never reached"),
            _node("n3", "end", "goodbye"),
        ],
        "edges": [_edge("e1", "n1", "n3", "done"), _edge("e2", "n2", "n3", "done too")],
    })
    warnings = graph_warnings(graph)
    assert [w.id for w in warnings] == ["n2"]


def test_undeclared_template_variable_is_a_warning():
    graph = parse_graph(_graph(nodes=[
        _node("n1", "start", "greeting", prompt="Hello {{ custmer_name }}"),
        _node("n2", "agent", "booking", prompt="ok"),
        _node("n3", "end", "goodbye"),
    ]))
    assert any("custmer_name" in w.message for w in graph_warnings(graph))


def test_declared_and_call_context_variables_do_not_warn():
    graph = parse_graph(_graph(nodes=[
        _node("n1", "start", "greeting", prompt="Hi {{ caller_number }}",
              extraction={"enabled": True, "variables": [{"name": "policy_number"}]}),
        _node("n2", "agent", "booking", prompt="Your policy {{ policy_number }}"),
        _node("n3", "end", "goodbye"),
    ]))
    assert graph_warnings(graph) == []


def test_render_substitutes_falls_back_and_never_leaks_braces():
    # An unrendered {{ x }} reaching TTS is the failure mode that ends up
    # in a call recording.
    assert render("Hi {{ name }}", {"name": "Ada"}) == "Hi Ada"
    assert render("Hi {{ name | there }}", {}) == "Hi there"
    assert render("Hi {{ name }}", {}) == "Hi "


def test_the_starter_graph_carries_the_tools_it_is_given():
    """Node.tools is default-deny, so migrate_workflow_text.py has to hand
    an existing agent's enabled tools through to the start node — the only
    non-terminal step a starter graph has. Without this an agent that could
    book yesterday silently cannot today."""
    graph = parse_graph(starter_graph("hi", "be nice", ["book_appointment"]))
    assert graph.start.tools == ["book_appointment"]
    # A brand-new agent has no policies yet and correctly gets none.
    assert parse_graph(starter_graph("hi", "be nice")).start.tools == []
