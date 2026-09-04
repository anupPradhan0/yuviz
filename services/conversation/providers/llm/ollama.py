"""
OllamaLLM — streaming text generation via a local Ollama instance.

Ollama exposes a REST API at http://localhost:11434.
Streaming is done over newline-delimited JSON (not SSE).

pip install httpx
Ollama must be running: https://ollama.com
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

_DEFAULT_BASE_URL = "http://localhost:11434"

# Ollama's own default keep_alive is 5 minutes — after that, an idle model
# is evicted from memory and the next request pays the full weight-load
# cost again. Confirmed live: a 36-minute gap between test calls
# produced a 14.8s first-turn LLM latency (vs ~270-300ms on every turn
# after) purely from qwen2.5:7b reloading — nothing to do with prompt
# content or TTS. 30m covers realistic gaps between calls on a single
# agent without holding memory indefinitely; raise/lower per how idle this
# deployment's traffic actually is.
_KEEP_ALIVE = "30m"


def _to_ollama_message(m: dict[str, Any]) -> dict[str, Any]:
    """build_chat_messages() yields a generic {role, content, tool_calls?,
    tool_call_id?} shape — bridge it to Ollama's native wire format here
    (nesting name/arguments under "function", per call, confirmed live
    2026-07-22), same as ToolDefinition.to_generic_schema() already gets
    bridged for the tools list. A "tool"-role message (tool_call_id set)
    passes through content as-is — Ollama expects a plain string there,
    unlike Gemini's structured functionResponse (see gemini.py)."""
    if not m.get("tool_calls"):
        return {"role": m["role"], "content": m["content"]}
    return {
        "role": m["role"],
        "content": m["content"],
        "tool_calls": [
            {"id": c["id"], "function": {"name": c["name"], "arguments": c["arguments"]}}
            for c in m["tool_calls"]
        ],
    }


class OllamaLLM:
    """
    ILLM implementation backed by Ollama's /api/chat endpoint.

    model       — any model pulled into the local Ollama instance
                  (e.g. "llama3", "mistral", "phi3", "gemma2")
    system      — system prompt prepended to every conversation
    temperature — sampling temperature (0.0 = deterministic)
    base_url    — Ollama server URL
    timeout_s   — per-token generation timeout in seconds
    """

    def __init__(
        self,
        model:       str = "llama3",
        system:      str = "You are a helpful voice assistant. "
                          "Keep responses concise and natural for speech.",
        temperature: float = 0.7,
        base_url:    str = _DEFAULT_BASE_URL,
        timeout_s:   float = 30.0,
    ) -> None:
        self._model       = model
        self._system      = system
        self._temperature = temperature
        self._base_url    = base_url.rstrip("/")
        self._timeout     = timeout_s
        self._client      = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
        )
        log.info("OllamaLLM model=%s base_url=%s", model, self._base_url)

    async def generate(self, messages: list[ChatMessage]) -> AsyncGenerator[str, None]:
        all_messages = build_chat_messages(self._system, messages)

        payload = {
            "model":   self._model,
            "messages": all_messages,
            "stream":  True,
            "options": {"temperature": self._temperature},
            "keep_alive": _KEEP_ALIVE,
        }

        async with self._client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("OllamaLLM: malformed JSON line=%r", line)
                    continue
                if data.get("done"):
                    break
                token = data.get("message", {}).get("content", "")
                if token:
                    yield token

    async def warm(self) -> None:
        # One real request so the model loads into memory before a live
        # call needs it — _KEEP_ALIVE only avoids reload on later idle
        # gaps, not the first request ever. Called from prewarm only.
        try:
            async for _ in self.generate([ChatMessage(role="user", content="Hi")]):
                pass
        except Exception:
            log.exception("OllamaLLM: warm() failed model=%s", self._model)

    async def generate_with_tools(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        """IToolAwareLLM companion to generate() — same client, same auth,
        additive method. Confirmed live 2026-07-22: with stream=true, a tool
        call arrives as ONE chunk carrying the complete message.tool_calls
        array (never built up token-by-token the way content is) — plain
        text content still streams incrementally either way, so a turn that
        doesn't call a tool keeps the exact same per-sentence TTS latency
        it has today.

        tool_choice: no real force-a-tool primitive on /api/chat, so a forced
        choice narrows the tools list to just that one instead — confirmed
        live, qwen2.5:7b reliably takes the only tool it's offered."""
        all_messages = [_to_ollama_message(m) for m in build_chat_messages(self._system, messages)]
        effective_schemas = schemas
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            forced_name = tool_choice.get("function", {}).get("name")
            narrowed = [s for s in schemas if s.get("name") == forced_name]
            if narrowed:
                effective_schemas = narrowed
        payload = {
            "model":    self._model,
            "messages": all_messages,
            "stream":   True,
            # Ollama's tool wire format matches OpenAI's — a generic
            # {name, description, parameters} schema wrapped in
            # {"type": "function", "function": ...}.
            "tools":    [{"type": "function", "function": s} for s in effective_schemas],
            "options":  {"temperature": self._temperature},
            "keep_alive": _KEEP_ALIVE,
        }

        async with self._client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("OllamaLLM: malformed JSON line=%r", line)
                    continue
                if data.get("done"):
                    break
                message = data.get("message", {})
                tool_calls = message.get("tool_calls") or []
                for i, call in enumerate(tool_calls):
                    fn = call.get("function", {})
                    yield ToolCallEvent(
                        tool_call_id=call.get("id") or f"call_{i}",
                        tool_name=fn.get("name", ""),
                        arguments=fn.get("arguments") or {},
                    )
                if tool_calls:
                    return
                token = message.get("content", "")
                if token:
                    yield TokenEvent(text=token)

    async def aclose(self) -> None:
        await self._client.aclose()
