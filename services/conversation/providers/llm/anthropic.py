"""
AnthropicLLM — streaming text generation via Anthropic's Messages API.

SSE over POST /v1/messages: every "data: " line carries a "type"
(content_block_start/_delta/_stop, message_stop, ping, error) rather than
OpenAI's choices[].delta or Gemini's candidates[].parts.

Four shapes this bridges: system is a top-level field; max_tokens is
required; there is no "tool" role (a result is a tool_result block on a
user turn, as in Gemini); tool arguments stream in as input_json_delta
fragments to concatenate by block index.

Raw httpx rather than the `anthropic` SDK, matching every other provider
here — requirements.txt is an exact pin-freeze verified from scratch.
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

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_API_VERSION = "2023-06-01"

# Claude 4.7-and-later removed the sampling parameters: sending temperature
# to one of these is a 400, not a warning (haiku-4-5 and the 4.6 family
# still accept it). The same generation also runs adaptive thinking when
# `thinking` is omitted, which on a voice turn spends the token budget
# reasoning instead of speaking. effort=low is the documented lever for
# that, and unlike thinking={"type": "disabled"} it is accepted across the
# whole family (Fable rejects "disabled" outright) and doesn't risk the
# tool-call-leaks-into-visible-text failure mode that disabling it has.
_EFFORT_MODELS = (
    "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5",
    "claude-opus-4-8", "claude-opus-4-7",
)


async def _raise_with_body_logged(resp: httpx.Response) -> None:
    """Same as openai.py/gemini.py's twin: raise_for_status() drops the body,
    which is the only place a 400 says what it rejected."""
    if resp.is_success:
        return
    body = await resp.aread()
    log.error("AnthropicLLM: HTTP %s error body=%s", resp.status_code, body.decode(errors="replace"))
    resp.raise_for_status()


class AnthropicLLM:
    """
    ILLM implementation backed by Anthropic's Messages endpoint.

    api_key    — resolved once at construction by AIProviderManager.
    model      — "claude-haiku-4-5" (cheap default: a turn here is a
                 sentence or two), "claude-sonnet-5", "claude-opus-5"
    system     — used only when the caller hasn't injected one, same
                 precedent as OllamaLLM.generate().
    max_tokens — required by the API; a ceiling on a runaway generation,
                 never a target. Thinking tokens draw from the same budget
                 on the models that think, hence the headroom.
    """

    def __init__(
        self,
        api_key:     str,
        model:       str = "claude-haiku-4-5",
        system:      str = "You are a helpful voice assistant. "
                          "Keep responses concise and natural for speech.",
        temperature: float = 0.7,
        max_tokens:  int = 4096,
        base_url:    str = _DEFAULT_BASE_URL,
        timeout_s:   float = 30.0,
    ) -> None:
        self._model       = model
        self._system      = system
        self._temperature = temperature
        self._max_tokens  = max_tokens
        # Key in a header, never the query string — see gemini.py.
        self._client      = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"x-api-key": api_key, "anthropic-version": _API_VERSION},
            timeout=timeout_s,
        )
        log.info("AnthropicLLM model=%s", model)

    def _shape_messages(self, messages: list[ChatMessage]) -> tuple[str | None, list[dict[str, Any]]]:
        """build_chat_messages() still owns the system-prompt precedence
        decision; this only re-shapes its output for Anthropic's wire
        format. Note tool_use "input" is a real object — OpenAI wants that
        same JSON as a string."""
        shaped = build_chat_messages(self._system, messages)
        system: str | None = None
        out: list[dict[str, Any]] = []
        for m in shaped:
            if m["role"] == "system":
                system = m["content"]
                continue
            if m.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                # An empty text block is a 400, and a tool-only turn has none.
                if m["content"]:
                    blocks.append({"type": "text", "text": m["content"]})
                blocks += [
                    {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                    for c in m["tool_calls"]
                ]
                out.append({"role": "assistant", "content": blocks})
                continue
            if m["role"] == "tool":
                out.append({"role": "user", "content": [{
                    "type":        "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "",
                    "content":     m["content"],
                }]})
                continue
            out.append({"role": m["role"], "content": m["content"]})
        return system, out

    def _payload(self, messages: list[ChatMessage]) -> dict[str, Any]:
        system, shaped = self._shape_messages(messages)
        payload: dict[str, Any] = {
            "model":      self._model,
            "messages":   shaped,
            "max_tokens": self._max_tokens,
            "stream":     True,
        }
        if self._model.startswith(_EFFORT_MODELS):
            payload["output_config"] = {"effort": "low"}
        else:
            payload["temperature"] = self._temperature
        if system:
            payload["system"] = system
        return payload

    def _decode(self, line: str) -> dict[str, Any] | None:
        """One "data: " line -> its JSON, or None for anything else."""
        if not line.startswith("data: "):
            return None
        try:
            data = json.loads(line[len("data: "):])
        except json.JSONDecodeError:
            log.warning("AnthropicLLM: malformed JSON line=%r", line)
            return None
        # An overloaded_error arrives as a 200 SSE line, not an HTTP status.
        # Raise rather than log-and-continue: swallowing it ends the turn in
        # silence, while _llm_to_tts already catches and speaks a fallback
        # line for any exception out of generate() (pipeline.py).
        if data.get("type") == "error":
            raise RuntimeError(f"AnthropicLLM: stream error event={data.get('error')}")
        return data

    async def generate(self, messages: list[ChatMessage]) -> AsyncGenerator[str, None]:
        # _shape_messages handles tool history even here: the orchestrator's
        # forced-final-generation replays it with no tools offered (openai.py).
        async with self._client.stream("POST", "/v1/messages", json=self._payload(messages)) as resp:
            await _raise_with_body_logged(resp)
            async for line in resp.aiter_lines():
                data = self._decode(line)
                if data is None or data.get("type") != "content_block_delta":
                    continue
                token = (data.get("delta") or {}).get("text") or ""
                if token:
                    yield token

    async def generate_with_tools(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]],
    ) -> AsyncGenerator[TurnEvent, None]:
        """IToolAwareLLM companion to generate() — same client/auth. The
        generic schema's "parameters" becomes "input_schema"; that rename is
        the only difference from what OpenAI/Gemini are handed."""
        payload = self._payload(messages)
        payload["tools"] = [
            {
                "name":         s["name"],
                "description":  s.get("description", ""),
                "input_schema": s.get("parameters") or {},
            }
            for s in schemas
        ]

        # Keyed by block index: a turn can open several tool_use blocks, and
        # their argument fragments interleave only by index, never by order.
        accumulating: dict[int, dict[str, Any]] = {}

        async with self._client.stream("POST", "/v1/messages", json=payload) as resp:
            await _raise_with_body_logged(resp)
            async for line in resp.aiter_lines():
                data = self._decode(line)
                if data is None:
                    continue
                event_type = data.get("type")
                index = data.get("index", 0)

                if event_type == "content_block_start":
                    block = data.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        accumulating[index] = {
                            "id": block.get("id"), "name": block.get("name"), "arguments": "",
                        }

                elif event_type == "content_block_delta":
                    delta = data.get("delta") or {}
                    if index in accumulating:
                        accumulating[index]["arguments"] += delta.get("partial_json") or ""
                        continue
                    token = delta.get("text") or ""
                    if token:
                        yield TokenEvent(text=token)

                elif event_type == "content_block_stop":
                    entry = accumulating.pop(index, None)
                    if entry is None:
                        continue
                    try:
                        args = json.loads(entry["arguments"]) if entry["arguments"] else {}
                    except json.JSONDecodeError:
                        log.warning("AnthropicLLM: malformed tool_use input=%r", entry["arguments"])
                        args = {}
                    yield ToolCallEvent(
                        tool_call_id=entry["id"] or f"call_{index}",
                        tool_name=entry["name"] or "",
                        arguments=args,
                    )

    async def aclose(self) -> None:
        await self._client.aclose()
