"""
The two background LLM passes a workflow makes over its own transcript
(docs/workflow.md §5.8 and §5.9), kept in one module because they are the
same shape: an out-of-band request on the call's own LLM, never in the
conversation context, never allowed to fail the call.

VariableExtraction turns a workflow from a router into something that
produces data — a node declares what to capture, and on leaving it an
extra LLM call reads the transcript and returns JSON that merges into the
runner's variables (so later prompts can say {{ policy_number }}) and lands
in calls.extracted_variables.

ContextSummarizer keeps a long, many-node call from carrying every tool
call belonging to nodes the conversation has already left. pipeline.py's
_trim_history is the cruder version of this and stays in place for
single-prompt agents; for a workflow it isn't enough, because dropping the
turns where the caller gave their date of birth loses information the
booking node still needs.

Both degrade to doing nothing. Extraction is analytics; summarization's
failure mode is "more tokens", which is always better than "lost the
caller's name".
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable

from libs.config_sdk.workflow import Node

from ..providers.interfaces import ChatMessage

log = logging.getLogger(__name__)

_EXTRACTION_TIMEOUT_S = 8.0
_SUMMARY_TIMEOUT_S = 8.0

# Above this many messages, a transition triggers a background
# summarization. Well clear of a normal few-turn stage — this is for the
# call that has been through five nodes and is carrying the tool traffic of
# all of them.
#
# It MUST stay below pipeline._trim_history's own cap (max_history * 2 + 1,
# so 21 at the default max_history=10), which is why the pipeline derives it
# rather than taking this default — see summary_threshold_for(). Chosen
# independently, the two drifted: at 24 vs 21, history could only cross the
# threshold transiently mid-turn on tool traffic, and by the time the
# background summary resolved trim had already cut back below the cutoff, so
# the apply-time guard bailed every time and the whole summarization half of
# this module was dead.
_SUMMARY_THRESHOLD_MSGS = 16


def summary_threshold_for(max_history: int) -> int:
    """The message count a transition summarizes above, for a pipeline that
    trims to `max_history` turn pairs. Kept a couple of exchanges under the
    trim cap so a summary is requested while there is still something for it
    to compress, and lands before trim would have discarded it outright.

    Clamped rather than just floored: at a very small max_history the floor
    would climb back above the cap and disable summarization again, which is
    the exact bug this function exists to make impossible. Below the cap it
    is a harmless no-op instead — _summarize's own `cutoff <= 1` guard
    returns before it asks the LLM for anything."""
    return min(max(_SUMMARY_KEEP_LAST + 2, max_history * 2 - 4), max_history * 2)
# Turns kept verbatim after the summary. The most recent exchange is what
# the model is actually responding to; paraphrasing it would be the one
# place summarization can visibly break a conversation.
_SUMMARY_KEEP_LAST = 4

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _strip_transition_noise(history: list[ChatMessage]) -> list[ChatMessage]:
    """Drop the transition tool traffic — dozens of {"status": "done"}
    results and their matching assistant calls accumulate over a long call
    and are pure noise to a model being asked what the caller said."""
    kept: list[ChatMessage] = []
    for msg in history:
        if msg.role == "tool" and '"status": "done"' in (msg.content or ""):
            continue
        if msg.role == "assistant" and msg.tool_calls and not (msg.content or "").strip():
            continue
        kept.append(msg)
    return kept


def _transcript(history: list[ChatMessage]) -> str:
    lines = [
        f"{msg.role}: {msg.content.strip()}"
        for msg in _strip_transition_noise(history)
        if msg.role in ("user", "assistant") and (msg.content or "").strip()
    ]
    return "\n".join(lines)


async def _collect(llm: Any, messages: list[ChatMessage], timeout_s: float) -> str:
    async def _run() -> str:
        chunks: list[str] = []
        async for token in llm.generate(messages):
            chunks.append(token)
        return "".join(chunks)

    return await asyncio.wait_for(_run(), timeout=timeout_s)


def _parse_json_object(text: str) -> dict[str, Any]:
    """Models wrap JSON in prose or a code fence often enough that not
    handling it means throwing away most successful extractions."""
    fenced = _JSON_FENCE_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object in extraction response: {text[:200]!r}")
    parsed = json.loads(candidate[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("extraction response was not a JSON object")
    return parsed


def _coerce(value: Any, declared_type: str) -> Any:
    if value is None or value == "":
        return None
    if declared_type == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if declared_type == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "yes", "1")
    return str(value)


class VariableExtractor:
    """Fire-and-forget by default (see extract()), with an explicit flush
    for the two moments something actually reads the values."""

    def __init__(
        self,
        llm: Any,
        on_variables: Callable[[dict[str, Any]], None],
        timeout_s: float = _EXTRACTION_TIMEOUT_S,
    ) -> None:
        self._llm = llm
        self._on_variables = on_variables
        self._timeout_s = timeout_s
        self._pending: set[asyncio.Task] = set()
        # Multiple teardown paths converge (caller hangs up, agent ends the
        # call, max duration, transfer completes). Without this guard they
        # race to write the same row twice.
        self._final_done = False

    @staticmethod
    def _wants_extraction(node: Node) -> bool:
        spec = node.extraction
        return spec is not None and spec.enabled and bool(spec.variables)

    def extract(self, node: Node, history: list[ChatMessage]) -> None:
        """Background by default: blocking a transition on an extraction
        round-trip adds a full LLM latency to a moment the caller is
        already waiting through."""
        if not self._wants_extraction(node):
            return
        task = asyncio.ensure_future(self._extract(node, list(history)))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def extract_final(self, node: Node, history: list[ChatMessage]) -> None:
        """Idempotent — see _final_done. Same guard as extract(): a call
        normally ends on an `end` node, which declares no extraction at all,
        and running one anyway would spend an LLM round-trip on teardown for
        every workflow call (and, before the guard, raise on the missing
        config)."""
        if self._final_done or not self._wants_extraction(node):
            return
        self._final_done = True
        await self._extract(node, list(history))

    async def flush(self) -> None:
        """Await whatever is still in flight. Called before anything that
        READS the values — transfer routing and call end — because a
        transfer that routes on {{ wants_callback }} cannot read a value
        still on the wire."""
        pending = list(self._pending)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)

    async def _extract(self, node: Node, history: list[ChatMessage]) -> None:
        try:
            spec = node.extraction
            wanted = "\n".join(
                f"- {v.name} ({v.type}): {v.prompt}" for v in spec.variables
            )
            instruction = (
                "You are extracting structured data from a phone call transcript.\n"
                f"{spec.prompt.strip()}\n\n" if spec.prompt.strip() else
                "You are extracting structured data from a phone call transcript.\n\n"
            )
            prompt = (
                f"{instruction}"
                f"Transcript so far:\n{_transcript(history)}\n\n"
                f"Extract these values:\n{wanted}\n\n"
                "Reply with a single JSON object whose keys are exactly the names above. "
                "Use null for anything the caller did not actually say — never guess."
            )
            raw = await _collect(
                self._llm, [ChatMessage(role="user", content=prompt)], self._timeout_s,
            )
            parsed = _parse_json_object(raw)
            values = {
                v.name: _coerce(parsed.get(v.name), v.type)
                for v in spec.variables
                if parsed.get(v.name) is not None
            }
            values = {k: v for k, v in values.items() if v is not None}
            if values:
                log.info("workflow: extracted %s at node=%s", sorted(values), node.name)
                self._on_variables(values)
        except asyncio.TimeoutError:
            log.warning("workflow: variable extraction timed out node=%s", node.name)
        except Exception:
            # Extraction is analytics. It never fails a call.
            log.exception("workflow: variable extraction failed node=%s", node.name)


class ContextSummarizer:
    """One background summarization at a time, applied by index snapshot at
    apply time (not request time) so messages added while it was generating
    survive."""

    def __init__(
        self,
        llm: Any,
        threshold_msgs: int = _SUMMARY_THRESHOLD_MSGS,
        keep_last: int = _SUMMARY_KEEP_LAST,
        timeout_s: float = _SUMMARY_TIMEOUT_S,
    ) -> None:
        self._llm = llm
        self._threshold = threshold_msgs
        self._keep_last = keep_last
        self._timeout_s = timeout_s
        self._task: asyncio.Task | None = None

    def maybe_summarize(self, history: list[ChatMessage]) -> None:
        if len(history) <= self._threshold:
            return
        # A second transition before the first summary landed makes that
        # summary stale — the conversation has moved on. Cancel rather than
        # let two of them race to splice the same list.
        self.cancel()
        self._task = asyncio.ensure_future(self._summarize(history))

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _summarize(self, history: list[ChatMessage]) -> None:
        try:
            cutoff = len(history) - self._keep_last
            # Never cut so that the retained tail STARTS with a tool result
            # whose assistant tool_calls message is on the deleted side —
            # OpenAI-shaped providers reject that request outright ("tool
            # must respond to a preceding tool_calls"), which would kill the
            # turn mid-call. Move the cut forward past any such orphan.
            while cutoff < len(history) and history[cutoff].role == "tool":
                cutoff += 1
            if cutoff <= 1 or cutoff >= len(history):
                return
            older = _strip_transition_noise(history[1:cutoff])
            if not older:
                return
            prompt = (
                "Summarize this part of an ongoing phone call in a short paragraph. "
                "Keep every concrete fact the caller gave — names, numbers, dates, "
                "what they asked for, what was agreed. Do not add anything they "
                "did not say.\n\n"
                + "\n".join(f"{m.role}: {m.content.strip()}" for m in older if (m.content or "").strip())
            )
            summary = (await _collect(
                self._llm, [ChatMessage(role="user", content=prompt)], self._timeout_s,
            )).strip()
            if not summary:
                return

            # Apply-time splice. `cutoff` still points at the same messages
            # it did at request time — everything that happened since was
            # appended past it — and re-reading len(history) here is what
            # keeps those new messages.
            if len(history) < cutoff or history[0].role != "system":
                return
            del history[1:cutoff]
            history.insert(1, ChatMessage(
                role="user",
                content=f"[Earlier in this call]\n{summary}",
            ))
            log.info("workflow: summarized %d earlier messages into context", cutoff - 1)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            # Keeping the full context is a worse token bill, not a worse
            # call. Degrading the other way loses information.
            log.warning("workflow: context summarization timed out — keeping full context")
        except Exception:
            log.exception("workflow: context summarization failed — keeping full context")
