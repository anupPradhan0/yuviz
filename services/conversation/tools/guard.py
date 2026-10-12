"""Checks that run before a tool is dispatched: scope scrubbing, argument validation, loop limits.

The model supplies arguments, never scope: tenant, agent and knowledge-base ids come from the session
(orchestrator/executor context), so any the model writes into a call are dropped here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from typing import Any

from jsonschema import Draft202012Validator

from .types import ToolResult, ToolStatus

log = logging.getLogger(__name__)

DEFAULT_MAX_TOOL_CALLS_PER_TURN = 12
# Calls refused in one turn before every tool is withdrawn and the model must answer in words.
_MAX_REFUSALS_PER_TURN = 3

# Lower-cased. Top level only: toolexec resolves execute_api `inputs` against the API's declared
# params and takes tenant/agent from the verified session, so nothing nested can widen scope.
_SCOPE_KEYS = frozenset({
    "tenant", "tenant_id", "tenant_slug", "organization_id", "org_id",
    "agent", "agent_id", "agent_slug",
    "kb", "kb_id", "kb_ids", "kb_slug", "kb_slugs",
    "knowledge_base", "knowledge_base_id", "knowledge_base_ids", "knowledge_base_slug",
})


def scrub_scope_args(arguments: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return (arguments without scope keys, the dropped key names)."""
    if not isinstance(arguments, dict):
        return arguments, []
    dropped = [k for k in arguments if isinstance(k, str) and k.lower() in _SCOPE_KEYS]
    if not dropped:
        return arguments, []
    return {k: v for k, v in arguments.items() if k not in dropped}, dropped


def _usable(schema: Any) -> Any:
    """An empty `enum` (execute_api before it is specialised for an agent) would reject every value."""
    if isinstance(schema, dict):
        return {k: _usable(v) for k, v in schema.items() if not (k == "enum" and v == [])}
    if isinstance(schema, list):
        return [_usable(v) for v in schema]
    return schema


def validate_json(value: Any, schema: dict[str, Any] | None) -> list[str]:
    """Short human-readable problems; empty = valid. A schema that cannot be evaluated never blocks a call."""
    if not schema:
        return []
    try:
        errors = sorted(Draft202012Validator(_usable(schema)).iter_errors(value), key=lambda e: list(e.path))
    except Exception:
        log.exception("tool schema could not be evaluated; skipping validation")
        return []
    return [
        f"{'.'.join(str(p) for p in e.path) or '(root)'}: {e.message}"[:200] for e in errors[:5]
    ]


def taint_blocks_writes() -> bool:
    """KNOWLEDGE_TAINT_BLOCKS_WRITES (default on). Off lets a write follow a document lookup in the same
    turn, at the cost of a planted passage being able to steer it."""
    return os.environ.get("KNOWLEDGE_TAINT_BLOCKS_WRITES", "1").strip().lower() not in ("0", "false", "no", "off")


def _canonical(arguments: Any) -> str:
    return json.dumps(arguments, sort_keys=True, default=str)


def call_signature(tool_name: str, arguments: Any) -> str:
    """Identity of one write for the confirmation memory: tool, api_name and a hash of the normalised arguments."""
    api = arguments.get("api_name", "") if isinstance(arguments, dict) else ""
    digest = hashlib.sha256(_canonical(arguments).encode()).hexdigest()[:24]
    return f"{tool_name}:{api}:{digest}"


class TurnGuard:
    """Per-turn state: call budget, repeated-identical-call detection, one repair attempt per tool."""

    def __init__(self, max_calls: int = DEFAULT_MAX_TOOL_CALLS_PER_TURN) -> None:
        self._max_calls = max_calls
        self._attempts = 0
        self._refusals = 0
        self._seen: set[tuple[str, str]] = set()
        self._per_tool: dict[str, int] = {}
        self._repairs: dict[str, int] = {}
        self.tainted = False  # untrusted retrieved text is in context for this user turn
        self.taint_refused = False  # a write was held this turn: stop offering writes, the model must ask

    @property
    def exhausted(self) -> bool:
        """True once the turn is looping: withdraw the tools so the model has to answer the caller."""
        return self._attempts >= self._max_calls or self._refusals >= _MAX_REFUSALS_PER_TURN

    def _refuse(self, error: str, payload: dict[str, Any] | None = None) -> ToolResult:
        self._refusals += 1
        status = ToolStatus.INVALID_ARGUMENT if error == "invalid_arguments" else ToolStatus.FAILED
        return ToolResult(status=status, error=error, payload=payload or {})

    def check(
        self, tool_name: str, arguments: dict[str, Any], schema: dict[str, Any] | None,
        max_calls_for_tool: int | None = None, detect_repeats: bool = True, side_effecting: bool = False,
        confirmed: bool = False,
    ) -> ToolResult | None:
        """None = proceed; otherwise the refusal to hand the model instead of executing."""
        self._attempts += 1
        if self._attempts > self._max_calls:
            return self._refuse("tool_call_cap_exceeded")

        problems = (
            ["arguments must be a JSON object"] if not isinstance(arguments, dict)
            else validate_json(arguments, schema)
        )
        if problems:
            repairs = self._repairs.get(tool_name, 0)
            self._repairs[tool_name] = repairs + 1
            if repairs >= 1:
                return self._refuse("invalid_arguments_unrecoverable")
            return self._refuse("invalid_arguments", {
                "problems": problems,
                "expected_schema": schema,
            })

        if side_effecting and self.tainted and taint_blocks_writes() and not confirmed:
            # Counts as a refusal and withdraws writes for the rest of the turn: the model has to answer in
            # words (ask the caller), not loop. The confirmation path is the next caller utterance.
            self.taint_refused = True
            return self._refuse("needs_caller_confirmation")

        key = (tool_name, _canonical(arguments))
        if detect_repeats and key in self._seen:
            return self._refuse("repeated_call")
        self._seen.add(key)

        count = self._per_tool.get(tool_name, 0) + 1
        self._per_tool[tool_name] = count
        if max_calls_for_tool is not None and count > max_calls_for_tool:
            return self._refuse("tool_call_cap_exceeded")
        return None


class LastGoodCache:
    """Short-lived last successful result of a read-only tool, served (flagged degraded) when the tool is down.

    The key carries tenant, agent and call id: a caller-specific lookup with empty inputs
    (e.g. "my orders") must never be replayed to another caller.
    """

    def __init__(self, ttl_s: float = 300.0, max_entries: int = 256) -> None:
        self._ttl_s = ttl_s
        self._max = max_entries
        self._entries: OrderedDict[tuple[str, ...], tuple[float, ToolResult]] = OrderedDict()

    @staticmethod
    def key(tenant_id: str, agent_id: str, call_id: str, tool_name: str, arguments: dict[str, Any]) -> tuple[str, ...]:
        return (tenant_id, agent_id, call_id, tool_name, _canonical(arguments))

    def put(self, key: tuple[str, ...], result: ToolResult) -> None:
        self._entries[key] = (time.monotonic(), result)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def get(self, key: tuple[str, ...]) -> ToolResult | None:
        hit = self._entries.get(key)
        if hit is None:
            return None
        stored_at, result = hit
        if time.monotonic() - stored_at > self._ttl_s:
            del self._entries[key]
            return None
        return result
