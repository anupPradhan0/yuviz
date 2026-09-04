"""
The two background LLM passes. Both are allowed to do nothing; neither is
ever allowed to break the call, so most of what's asserted here is what
happens when things go wrong.
"""

from __future__ import annotations

import asyncio

from libs.config_sdk.workflow import Extraction, ExtractionVariable, Node

from services.conversation.providers.interfaces import ChatMessage
from services.conversation.workflow import ContextSummarizer, VariableExtractor


class _FakeLLM:
    def __init__(self, reply: str = "{}") -> None:
        self._reply = reply
        self.calls: list[list[ChatMessage]] = []

    async def generate(self, messages):
        self.calls.append(list(messages))
        yield self._reply


def _node(**extraction) -> Node:
    """An agent node that declares one string variable unless told otherwise."""
    spec = Extraction(
        enabled=extraction.get("enabled", True),
        prompt="",
        variables=tuple(
            ExtractionVariable(name=n, type=t, prompt="")
            for n, t in extraction.get("variables", [("policy_number", "string")])
        ),
    )
    return Node(id="n1", type="agent", name="verify", prompt="", extraction=spec)


def test_extraction_merges_typed_values():
    llm = _FakeLLM('```json\n{"policy_number": "AB-1", "wants_callback": "yes", "unsaid": null}\n```')
    got: dict = {}
    extractor = VariableExtractor(llm, got.update)

    asyncio.run(extractor._extract(
        _node(variables=[("policy_number", "string"), ("wants_callback", "boolean"), ("unsaid", "string")]),
        [ChatMessage(role="user", content="my policy is AB-1")],
    ))

    # null means "the caller didn't say it" — never merged, so it can't
    # blank out something an earlier node captured.
    assert got == {"policy_number": "AB-1", "wants_callback": True}


def test_a_node_with_no_extraction_config_never_calls_the_llm():
    # A call normally ends on an `end` node, which declares nothing. Running
    # one anyway would spend a round-trip on teardown for every call.
    llm = _FakeLLM()
    extractor = VariableExtractor(llm, lambda _: None)
    end_node = Node(id="n9", type="end", name="goodbye", prompt="")

    asyncio.run(extractor.extract_final(end_node, []))
    extractor.extract(end_node, [])

    assert llm.calls == []


def test_extraction_disabled_is_honored_at_teardown_too():
    llm = _FakeLLM()
    extractor = VariableExtractor(llm, lambda _: None)
    asyncio.run(extractor.extract_final(_node(enabled=False), []))
    assert llm.calls == []


def test_the_final_pass_runs_once_however_many_teardowns_converge():
    llm = _FakeLLM('{"policy_number": "AB-1"}')
    extractor = VariableExtractor(llm, lambda _: None)

    async def _both():
        await extractor.extract_final(_node(), [])
        await extractor.extract_final(_node(), [])

    asyncio.run(_both())
    assert len(llm.calls) == 1


def test_a_broken_llm_reply_never_raises():
    llm = _FakeLLM("I'm afraid I can't do that.")
    extractor = VariableExtractor(llm, lambda _: None)
    asyncio.run(extractor._extract(_node(), []))   # logged, not raised


def _long_history(n: int) -> list[ChatMessage]:
    history = [ChatMessage(role="system", content="prompt")]
    for i in range(n):
        history.append(ChatMessage(role="user", content=f"caller {i}"))
        history.append(ChatMessage(role="assistant", content=f"agent {i}"))
    return history


def test_summary_replaces_the_older_half_and_keeps_the_system_prompt():
    history = _long_history(15)
    summarizer = ContextSummarizer(_FakeLLM("They gave their DOB and postcode."), keep_last=4)

    asyncio.run(summarizer._summarize(history))

    assert history[0].role == "system"
    assert "They gave their DOB" in history[1].content
    assert history[-1].content == "agent 14"       # recent turns kept verbatim
    assert len(history) == 6


def test_summary_never_orphans_a_tool_result():
    # A tool result whose assistant tool_calls parent was deleted makes
    # OpenAI-shaped providers reject the whole request — which would kill
    # the turn mid-call, the one failure mode summarization must not have.
    history = _long_history(10)
    history.append(ChatMessage(role="assistant", content="", tool_calls=[{"id": "c1", "name": "book"}]))
    history.append(ChatMessage(role="tool", content='{"status":"ok"}', tool_call_id="c1"))
    history.append(ChatMessage(role="assistant", content="Booked."))
    summarizer = ContextSummarizer(_FakeLLM("Earlier context."), keep_last=2)

    asyncio.run(summarizer._summarize(history))

    tool_indexes = [i for i, m in enumerate(history) if m.role == "tool"]
    for i in tool_indexes:
        prior = history[i - 1]
        assert prior.role == "assistant" and prior.tool_calls, "tool result lost its call"


def test_a_failing_summary_keeps_the_full_context():
    class _Broken:
        async def generate(self, messages):
            raise RuntimeError("provider down")
            yield ""   # pragma: no cover

    history = _long_history(15)
    before = list(history)
    asyncio.run(ContextSummarizer(_Broken())._summarize(history))
    # Degrading to "more tokens" beats degrading to "lost the caller's name".
    assert history == before


def test_the_summary_threshold_stays_under_the_pipelines_trim_cap():
    """The two are the same constraint and were chosen independently, which
    is how they drifted (24 vs a 21-message cap) and left summarization
    unable to ever apply: history could only cross the threshold transiently
    mid-turn, and trim had cut back below the cutoff by the time the
    background summary resolved."""
    from services.conversation.workflow import summary_threshold_for

    for max_history in (1, 5, 10, 40):
        trim_cap = max_history * 2 + 1      # pipeline._trim_history
        assert summary_threshold_for(max_history) < trim_cap, max_history
