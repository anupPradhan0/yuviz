from __future__ import annotations

import logging
from typing import Any

import httpx

from ..interfaces import ChatMessage


async def raise_with_body_logged(resp: httpx.Response, *, log: logging.Logger, provider: str) -> None:
    """httpx.Response.raise_for_status() never surfaces the response body,
    so a 4xx/429 here otherwise reaches the caller as a bare 'Client error'
    with no indication of what the vendor actually rejected — confirmed
    live, repeatedly, across providers (Groq 429s and 400s, a Gemini 400
    deep into a tool-calling turn, both with no visible detail otherwise).
    Read the body before raising so the next occurrence is diagnosable
    from the log alone. Shared by every ILLM provider; only the logger and
    provider name differ."""
    if resp.is_success:
        return
    body = await resp.aread()
    log.error("%s: HTTP %s error body=%s", provider, resp.status_code, body.decode(errors="replace"))
    resp.raise_for_status()


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
