from __future__ import annotations

from typing import Any

from ..interfaces import ChatMessage


def build_chat_messages(system: str, messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Prepend `system` as a system-role message, unless the caller already
    injected one via `messages` (PipelineConversationHandler manages
    per-agent system prompts that way) — prepending both would send
    contradictory instructions. Shared by every ILLM provider; the wire
    format each sends this over (NDJSON, SSE, ...) differs downstream, but
    the message assembly itself is identical.

    tool_calls/tool_call_id (see ChatMessage) pass through unchanged when
    present — plain generate() implementations never set them and never
    look at them; only generate_with_tools() implementations translate
    them into each vendor's own native tool-result wire shape."""
    has_system = any(m.role == "system" for m in messages)
    result: list[dict[str, Any]] = []
    if system and not has_system:
        result.append({"role": "system", "content": system})
    for m in messages:
        entry: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls is not None:
            entry["tool_calls"] = m.tool_calls
        if m.tool_call_id is not None:
            entry["tool_call_id"] = m.tool_call_id
        result.append(entry)
    return result
