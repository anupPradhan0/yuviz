"""Run a parse in a child Python process: a hostile file can hang, exhaust memory or crash a C extension, and
none of that may take the ingestion worker down with it.

Isolation is a plain subprocess (portable on macOS and Linux, no extra dependency) with:
  - a wall-clock timeout, enforced here by killing the child
  - RLIMIT_CPU everywhere and RLIMIT_AS (memory) on Linux, set by the child on itself
  - a scrubbed environment (PATH only), so the child cannot read service secrets
  - the document on stdin and a JSON result on stdout; the child writes no files
It is not a security boundary against a kernel exploit; production hardening (seccomp, container) is a deploy concern."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path

from .limits import Limits
from .model import ParsedDocument, ParseRejected

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MAX_OUTPUT = 200 * 1024 * 1024


async def run_parse(raw: bytes, content_type: str, limits: Limits, language: str | None = None) -> ParsedDocument:
    args = json.dumps({"content_type": content_type, "language": language, "limits": dataclasses.asdict(limits)})
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-X", "utf8", "-m", "services.knowledge.parsers._worker", args,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        cwd=str(_REPO_ROOT), env=env, limit=_MAX_OUTPUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(raw), timeout=limits.timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ParseRejected(f"parsing timed out after {limits.timeout_s:g}s (file too large or complex)") from None
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    try:
        result = json.loads(stdout)
    except ValueError:
        # Killed by RLIMIT_CPU/RLIMIT_AS or crashed in a C extension: no JSON came back.
        raise ParseRejected("parser stopped unexpectedly (file too large or complex to read safely)") from None
    if not result.get("ok"):
        raise ParseRejected(result.get("reason") or "file could not be read")
    return ParsedDocument.from_json(result["doc"])
