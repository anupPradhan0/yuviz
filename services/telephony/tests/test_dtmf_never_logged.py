"""Caller keypresses must never reach a log line.

A `collect` node is how an IVR takes a PIN or a card number, so the digit
value is caller-entered secret material. This rule was established — and
tripwired — in services/vobiz/bridge.py; that service was replaced by
services/telephony/ and libs/media_stream_sdk/, the tripwire went with it,
and the webhook DTMF path (c906145) then logged `digit=%s` at INFO with
nothing left to catch it. This restores the tripwire over every place a
digit now enters the platform.

Presence-only is fine ("dtmf received call=…"); the digit is not, in any
form — not a last digit, not a length.

The scan parses each module rather than matching lines: a regex over single
lines misses the multi-line call style most of these modules use, a digit
passed under another label (`key=%s`), and plurals (`digits=%s`) — exactly
the ways the leak would come back.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]

# Every module a keypress passes through, from the carrier to where a
# `collect` node assembles the PIN: the webhook parsers (telephony_sdk), the
# media-stream and telephony services that receive it, and the conversation
# service that turns it into IVR input (servicer -> session.push_dtmf ->
# callflow/runner). If DTMF gains a new entry point, add it here — an entry
# point this list omits is one this test cannot see.
_DTMF_ENTRY_POINTS = [
    _REPO / "libs" / "telephony_sdk",
    _REPO / "libs" / "media_stream_sdk",
    _REPO / "services" / "telephony",
    _REPO / "services" / "conversation",
]

_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
# A string that labels a digit-named field, e.g. "digit=%s", "digits: {}".
_DIGIT_LABEL = re.compile(r"digits?\s*[=:]", re.IGNORECASE)


def _is_logger(node: ast.expr) -> bool:
    """`log`, `logger`, `_log`, `LOG`, `logging`, `self.log`, `self._logger`, …"""
    if isinstance(node, ast.Name):
        return "log" in node.id.lower()
    if isinstance(node, ast.Attribute):
        return "log" in node.attr.lower()
    return False


def _log_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in _LOG_METHODS
        and _is_logger(n.func.value)
    ]


def _leaks_a_digit(call: ast.Call) -> bool:
    for arg in [*call.args, *(kw.value for kw in call.keywords)]:
        for node in ast.walk(arg):
            if isinstance(node, ast.Name) and "digit" in node.id.lower():
                return True
            if isinstance(node, ast.Attribute) and "digit" in node.attr.lower():
                return True
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _DIGIT_LABEL.search(node.value):
                return True
    return False


def _modules(root: Path) -> list[Path]:
    return [
        p for p in sorted(root.rglob("*.py"))
        if "tests" not in p.parts and "__pycache__" not in p.parts
    ]


def test_the_entry_points_exist_and_contain_log_calls():
    # Guard against the vacuous pass: if a directory is renamed or moved,
    # the scan below finds nothing and "no leaks" means nothing.
    for root in _DTMF_ENTRY_POINTS:
        assert root.is_dir(), f"{root} is gone — update _DTMF_ENTRY_POINTS"
        scanned = sum(len(_log_calls(p)) for p in _modules(root))
        assert scanned > 0, f"no log calls found under {root}; the scan is not looking anywhere"


def test_no_log_call_passes_a_dtmf_digit():
    leaks = [
        f"{path.relative_to(_REPO)}:{call.lineno}: {ast.unparse(call)}"
        for root in _DTMF_ENTRY_POINTS
        for path in _modules(root)
        for call in _log_calls(path)
        if _leaks_a_digit(call)
    ]
    assert not leaks, (
        "a caller keypress is written to a log line — a PIN entered at a "
        "`collect` node would be recoverable from logs. Log presence only:\n  "
        + "\n  ".join(leaks)
    )


# The detector itself, against the shapes a line-based scan let through.
_LEAKY = [
    'log.info("dtmf received call=%s digit=%s", cid, digit)',
    'self.log.info(\n    "dtmf received call=%s digit=%s",\n    cid,\n    digit,\n)',
    'log.info("dtmf received call=%s key=%s", cid, digit)',
    'log.info("telephony.dtmf.sent digits=%s", dtmf_digit)',
    'logger.debug(f"pressed {event.digit}")',
    'log.info("collected %d keys", len(digits))',
    'logging.warning("digits: %s", buf)',
]
_CLEAN = [
    'log.info("dtmf received call=%s", cid)',
    'log.info("collect node %s complete", node_id)',
    'self.log.debug("dtmf ignored: no live session")',
]


def test_the_detector_flags_the_known_leak_shapes():
    for src in _LEAKY:
        calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)]
        assert any(_leaks_a_digit(c) for c in calls), f"not flagged: {src!r}"
    for src in _CLEAN:
        calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)]
        assert not any(_leaks_a_digit(c) for c in calls), f"false positive: {src!r}"
