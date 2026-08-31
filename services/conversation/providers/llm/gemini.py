"""
GeminiLLM — streaming text generation via Google's Gemini API.

Streaming is Server-Sent Events (SSE) over the streamGenerateContent
endpoint: lines prefixed "data: ", each a JSON chunk carrying an
incremental candidates[0].content.parts[].text — same token-yielding
contract as OllamaLLM/OpenAILLM, different wire format.

Gemini's message shape differs from OpenAI/Ollama's in three ways this
class has to bridge, not push onto callers: (1) the system prompt is a
top-level system_instruction field, never a "system"-role message inside
contents; (2) the assistant's own role is named "model", not "assistant";
(3) a functionCall part returned with tool-calling carries a sibling
"thoughtSignature" field (Gemini's "thinking" models) that MUST be
echoed back verbatim on that same functionCall
part when it's replayed into history for a later turn — omitting it is a
hard 400 (INVALID_ARGUMENT: "missing a thought_signature"), not a
degraded-quality warning. Carried end-to-end via ToolCallEvent/ChatMessage's
generic provider_metadata passthrough (see llm_adapter.py) so the
orchestrator never has to know this exists.
build_chat_messages() still owns the "does the caller already have a
system message" precedence decision — this class only re-shapes its
output for Gemini's wire format afterward.

Auth is the x-goog-api-key header — never the "key" query parameter, which
httpx logs at INFO (see generate()).

pip install httpx (already a dependency via OllamaLLM)
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator

import httpx

from ..interfaces import ChatMessage
from . import build_chat_messages
from ...tools.llm_adapter import TokenEvent, ToolCallEvent, TurnEvent

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"


async def _raise_with_body_logged(resp: httpx.Response) -> None:
    """httpx.Response.raise_for_status() never surfaces the response body,
    so a 4xx here otherwise reaches the caller as a bare 'Client error 400'
    with no indication of which part of the payload Gemini rejected — a
    real 400 on a late-conversation turn (several retried tool calls deep)
    gave no actionable detail. Read the
    body before raising so the next occurrence is diagnosable from the log
    alone."""
    if resp.is_success:
        return
    body = await resp.aread()
    log.error("GeminiLLM: HTTP %s error body=%s", resp.status_code, body.decode(errors="replace"))
    resp.raise_for_status()


def _tool_name_for_call_id(shaped: list[dict[str, Any]], tool_call_id: str | None) -> str:
    """Gemini's functionResponse needs the original call's tool name, which
    a "tool"-role ChatMessage only references indirectly via
    tool_call_id — looked up from whichever earlier message's tool_calls
    entry has a matching id (a tool-result message always follows its own
    assistant tool_calls message in a well-formed history)."""
    for m in shaped:
        for call in m.get("tool_calls") or []:
            if call.get("id") == tool_call_id:
                return call.get("name", "")
    return ""


class GeminiLLM:
    """
    ILLM implementation backed by Google's Gemini streamGenerateContent endpoint.

    api_key     — resolved once at construction by AIProviderManager via
                  SecretResolver, never re-resolved per call.
    model       — e.g. "gemini-flash-latest" (an alias Google keeps pointed
                  at its current stable fast model — recommended default,
                  since pinned version strings get deprecated for new
                  callers over time), "gemini-2.5-pro"
    system      — system prompt sent as Gemini's system_instruction when
                  the caller hasn't already injected one (same precedent
                  as OllamaLLM.generate()).
    temperature — sampling temperature (0.0 = deterministic)
    """

    def __init__(
        self,
        api_key:     str,
        model:       str = "gemini-flash-latest",
        system:      str = "You are a helpful voice assistant. "
                          "Keep responses concise and natural for speech.",
        temperature: float = 0.7,
        base_url:    str = _DEFAULT_BASE_URL,
        # 30s left a caller sitting in dead air for a full 30 seconds
        # before any fallback spoke — Gemini's
        # streamGenerateContent endpoint occasionally never sends even its
        # first byte (httpx.ReadTimeout while still waiting on response
        # headers, not a slow-but-progressing stream). 10s cuts that wait
        # dramatically; see generate()/generate_with_tools()'s retry-once
        # logic for why this is safe to shorten without losing legitimate
        # slow-but-working requests.
        timeout_s:   float = 10.0,
    ) -> None:
        self._model       = model
        self._system      = system
        self._temperature = temperature
        self._api_key      = api_key
        self._client      = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)
        log.info("GeminiLLM model=%s", model)

    def _shape_contents(self, messages: list[ChatMessage]) -> tuple[str | None, list[dict[str, Any]]]:
        """Bridges two Gemini-specific shapes build_chat_messages()'s generic
        output doesn't know about: a tool_calls-bearing assistant message
        becomes a functionCall part (args, not "arguments"), and a
        "tool"-role message becomes a functionResponse
        part on a "user"-role turn — Gemini has no distinct tool role at
        all. functionResponse.response must be a JSON object, not a string,
        so a tool message's content (always a JSON string by convention —
        see ChatMessage's docstring) is parsed back into one here."""
        shaped = build_chat_messages(self._system, messages)
        system_instruction = None
        contents: list[dict[str, Any]] = []
        for m in shaped:
            if m["role"] == "system":
                system_instruction = m["content"]
                continue
            if m.get("tool_calls"):
                parts = []
                for c in m["tool_calls"]:
                    part: dict[str, Any] = {"functionCall": {"name": c["name"], "args": c["arguments"]}}
                    sig = (c.get("provider_metadata") or {}).get("thought_signature")
                    if sig:
                        part["thoughtSignature"] = sig
                    parts.append(part)
                contents.append({"role": "model", "parts": parts})
                continue
            if m["role"] == "tool":
                try:
                    response_obj = json.loads(m["content"]) if m["content"] else {}
                except json.JSONDecodeError:
                    response_obj = {"result": m["content"]}
                contents.append({"role": "user", "parts": [{"functionResponse": {
                    "name": _tool_name_for_call_id(shaped, m.get("tool_call_id")), "response": response_obj,
                }}]})
                continue
            role = "model" if m["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
        return system_instruction, contents

    async def generate(self, messages: list[ChatMessage]) -> AsyncGenerator[str, None]:
        system_instruction, contents = self._shape_contents(messages)

        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": self._temperature},
        }
        if system_instruction:
            payload["system_instruction"] = {"parts": [{"text": system_instruction}]}

        path = f"/v1beta/models/{self._model}:streamGenerateContent"
        # Key in the header, never the query string: httpx logs full URLs at
        # INFO, so ?key=... put a live key in the container logs.
        params = {"alt": "sse"}
        headers = {"x-goog-api-key": self._api_key}

        # Retry once, but only if the timeout hit before a single token came
        # back — Gemini's streamGenerateContent occasionally never sends
        # even its first byte. A retry after
        # tokens have already reached the caller/TTS would duplicate or
        # corrupt the turn, so it's only safe here, before anything's been
        # yielded yet.
        for attempt in range(2):
            yielded_any = False
            try:
                async with self._client.stream("POST", path, params=params, headers=headers, json=payload) as resp:
                    await _raise_with_body_logged(resp)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[len("data: "):]
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            log.warning("GeminiLLM: malformed JSON line=%r", line)
                            continue
                        candidates = data.get("candidates") or []
                        if not candidates:
                            continue
                        for part in candidates[0].get("content", {}).get("parts", []):
                            token = part.get("text", "")
                            if token:
                                yielded_any = True
                                yield token
                return
            except httpx.TimeoutException:
                if yielded_any or attempt == 1:
                    raise
                log.warning("GeminiLLM: request timed out with no output yet, retrying once")

    async def generate_with_tools(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]],
    ) -> AsyncGenerator[TurnEvent, None]:
        """IToolAwareLLM companion to generate() — same client/auth, additive
        method. Gemini wraps the generic {name, description, parameters}
        schema list into a single functionDeclarations entry, unlike
        OpenAI/Ollama's per-tool wrapper. A functionCall part, like a text
        part, arrives as a complete unit in whichever chunk it appears —
        never built up incrementally the way text streams — so plain-text
        turns keep the same per-sentence TTS latency they have today."""
        system_instruction, contents = self._shape_contents(messages)

        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": self._temperature},
            "tools": [{"functionDeclarations": schemas}],
        }
        if system_instruction:
            payload["system_instruction"] = {"parts": [{"text": system_instruction}]}

        path = f"/v1beta/models/{self._model}:streamGenerateContent"
        # Key in the header, never the query string: httpx logs full URLs at
        # INFO, so ?key=... put a live key in the container logs.
        params = {"alt": "sse"}
        headers = {"x-goog-api-key": self._api_key}

        # Same retry-once-if-nothing-yielded-yet posture as generate() —
        # see its comment for why this is only safe before any output
        # (token or tool call) has reached the caller.
        for attempt in range(2):
            yielded_any = False
            try:
                async with self._client.stream("POST", path, params=params, headers=headers, json=payload) as resp:
                    await _raise_with_body_logged(resp)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[len("data: "):]
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            log.warning("GeminiLLM: malformed JSON line=%r", line)
                            continue
                        candidates = data.get("candidates") or []
                        if not candidates:
                            continue
                        saw_tool_call = False
                        for i, part in enumerate(candidates[0].get("content", {}).get("parts", [])):
                            fn_call = part.get("functionCall")
                            if fn_call:
                                saw_tool_call = True
                                yielded_any = True
                                sig = part.get("thoughtSignature")
                                yield ToolCallEvent(
                                    tool_call_id=fn_call.get("id") or f"call_{i}",
                                    tool_name=fn_call.get("name", ""),
                                    arguments=fn_call.get("args") or {},
                                    provider_metadata={"thought_signature": sig} if sig else None,
                                )
                                continue
                            token = part.get("text", "")
                            if token:
                                yielded_any = True
                                yield TokenEvent(text=token)
                        if saw_tool_call:
                            return
                return
            except httpx.TimeoutException:
                if yielded_any or attempt == 1:
                    raise
                log.warning("GeminiLLM: request timed out with no output yet, retrying once")

    async def aclose(self) -> None:
        await self._client.aclose()
