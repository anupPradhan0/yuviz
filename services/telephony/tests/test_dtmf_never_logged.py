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
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]

# Every module that receives a keypress from a carrier and hands it to the
# conversation service. If DTMF gains a new entry point, add it here — an
# entry point this list omits is one this test cannot see.
_DTMF_ENTRY_POINTS = [
    _REPO / "services" / "telephony",
    _REPO / "libs" / "media_stream_sdk",
]

# A logging call whose format string interpolates something digit-named.
# Matches `digit=%s`, `digit=%r`, `digit: %s`, and the f-string forms
# `{digit}` / `{dtmf_digit}` inside a log call.
_LOG_CALL = re.compile(r"\b(?:log|logger|self\.log)\.\w+\(")
_DIGIT_FIELD = re.compile(r"digit\s*[=:]\s*%[srd]|\{\s*\w*digit\w*\s*[!:}]", re.IGNORECASE)


def _log_lines(root: Path) -> list[tuple[Path, int, str]]:
    hits = []
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _LOG_CALL.search(line):
                hits.append((path, lineno, line))
    return hits


def test_the_entry_points_exist_and_contain_log_calls():
    # Guard against the vacuous pass: if a directory is renamed or moved,
    # the scan below finds nothing and "no leaks" means nothing.
    for root in _DTMF_ENTRY_POINTS:
        assert root.is_dir(), f"{root} is gone — update _DTMF_ENTRY_POINTS"
        assert _log_lines(root), f"no log calls found under {root}; the scan is not looking anywhere"


def test_no_log_call_interpolates_a_dtmf_digit():
    leaks = [
        f"{path.relative_to(_REPO)}:{lineno}: {line.strip()}"
        for root in _DTMF_ENTRY_POINTS
        for path, lineno, line in _log_lines(root)
        if _DIGIT_FIELD.search(line)
    ]
    assert not leaks, (
        "a caller keypress is written to a log line — a PIN entered at a "
        "`collect` node would be recoverable from logs. Log presence only:\n  "
        + "\n  ".join(leaks)
    )
