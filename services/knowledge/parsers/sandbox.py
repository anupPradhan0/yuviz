"""Run a parse in a child Python process: a hostile file can hang, exhaust memory or crash a C extension, and
none of that may take the ingestion worker down with it.

WHAT THIS IS: a resource-limited, isolated-interpreter child. It is NOT a sandbox against a compromised parser.
A document that exploits lxml, Pillow or pypdf gets code execution as the SAME OS USER as the worker, so the
in-process steps below only raise the bar. Full containment needs a separate uid or container with a read-only
filesystem, no network and none of the service's secrets; see docs/knowledge-parser-sandbox.md.

What the child gets:
  - `python -I`: no cwd, script dir, PYTHONPATH or user site on sys.path
  - cwd = a fresh empty temp dir, removed afterwards; close_fds; its own session and process group
  - an environment of PATH and LANG only (no DB DSN, no JWT secret)
  - wall-clock timeout, output byte cap and an RSS watchdog (polls `ps`, so it works on macOS and Linux); any breach
    SIGKILLs the whole process group, so an OCR grandchild dies with it
  - in the child (Linux, best effort, see _worker.harden): RLIMIT_CPU/FSIZE/CORE/NOFILE/AS/NPROC, no_new_privs,
    non-dumpable, and a private (empty) network namespace when the kernel allows it
Not covered: the child can still read any file the worker's uid can read, and (where unshare is refused) use the
network. The worker process itself is made non-dumpable at startup (hardening.py) so the child cannot read its
environment or memory through /proc."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Callable

from .limits import Limits
from .model import ParsedDocument, ParseInfrastructureError, ParseRejected

log = logging.getLogger(__name__)

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
_WORKER = str(Path(__file__).with_name("_worker.py"))
_PS = shutil.which("ps")
_RSS_POLL_S = 0.02
MIN_MEMORY_MB = 256            # below this the child cannot even import its libraries (every document would dead-letter)
EXIT_MEMORY = 75               # the child's own memory guard exits with this
_STDERR_TAIL = 4096
_CPU_SIGNALS = {signal.SIGXCPU}


def effective_limits(limits: Limits) -> Limits:
    """A memory limit below MIN_MEMORY_MB is raised to it, with a warning: set too low, the child's address-space limit
    makes its imports fail and every document is retried and dead-lettered."""
    if limits.memory_mb < MIN_MEMORY_MB:
        log.warning("KB_PARSE_MEMORY_MB=%s is below the minimum workable %s; using %s", limits.memory_mb, MIN_MEMORY_MB, MIN_MEMORY_MB)
        return dataclasses.replace(limits, memory_mb=MIN_MEMORY_MB)
    return limits


def _child_env(nonce: str) -> dict[str, str]:
    # KB_PARSE_NONCE is not a secret: it lets the parent tell its own child's hardening notes from document text.
    return {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "KB_PARSE_NONCE": nonce}


async def spawn(tail: list[str], cwd: str, nonce: str = "") -> asyncio.subprocess.Process:
    """The one place the child is started, so tests exercise exactly the production isolation settings."""
    return await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-X", "utf8", *tail,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=cwd, env=_child_env(nonce), close_fds=True, start_new_session=True,
    )


async def _group_rss_mb(pgid: int) -> float | None:
    """Resident memory of the whole process group (the parser and anything it started), in MB. `ps -axo` works the
    same on macOS and Linux, so this is the one memory cap that holds on both."""
    if _PS is None:
        return None
    try:
        p = await asyncio.create_subprocess_exec(
            _PS, "-axo", "pgid=,rss=", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await p.communicate()
        total = 0
        for line in out.decode("ascii", "replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == str(pgid):
                total += int(parts[1])
        return total / 1024
    except (OSError, ValueError):
        return None


async def _exited(proc: asyncio.subprocess.Process) -> None:
    """Returns when the child itself has exited. proc.wait() also waits for every inherited pipe to close, and a
    grandchild that holds them open would stall it until the timeout."""
    while proc.returncode is None:
        await asyncio.sleep(0.02)


def _killpg(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    while chunk := await stream.read(65536):
        total += len(chunk)
        if total > cap:
            return b"", True
        chunks.append(chunk)
    return b"".join(chunks), False


async def _read_tail(stream: asyncio.StreamReader) -> bytes:
    tail = b""
    while chunk := await stream.read(65536):
        tail = (tail + chunk)[-_STDERR_TAIL:]
    return tail


async def _feed(proc: asyncio.subprocess.Process, raw: bytes) -> None:
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        proc.stdin.write(raw)
        await proc.stdin.drain()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
        proc.stdin.close()


_SAFE_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,59}")


def _printable(text: str, limit: int = 160) -> str:
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in text)[:limit]


def _log_stderr(rc: int | None, tail: bytes, failed: bool, nonce: str) -> None:
    """Child stderr is partly document text, so none of it is logged as itself. Only lines carrying this run's nonce
    (written by our own _worker.harden) are logged, escaped and capped; on failure the return code or signal and the
    final exception CLASS (validated as an identifier) are logged. Everything else is dropped."""
    marker = f"hardening[{nonce}]: "
    lines = tail.decode("utf-8", "replace").splitlines()
    for ln in lines:
        if nonce and ln.startswith(marker):
            log.warning("parse sandbox note (from the parser process): %s", _printable(ln[len(marker):]))
    if failed:
        last = next((ln for ln in reversed(lines) if ln.strip()), "")
        cls = last.split(":", 1)[0] if ":" in last else ""
        cls = cls if _SAFE_CLASS.fullmatch(cls) else "?"
        sig = signal.Signals(-rc).name if rc is not None and rc < 0 else None
        log.error("parse subprocess failed rc=%s signal=%s last_error_class=%s", rc, sig, cls)


async def run_parse(
    raw: bytes, content_type: str, limits: Limits, language: str | None = None, *,
    _child_tail: list[str] | None = None, _prepare: Callable[[str], None] | None = None,
) -> ParsedDocument:
    """`_child_tail` / `_prepare` exist for tests: run other child code under the same supervision, and plant files
    in the child's cwd before it starts."""
    limits = effective_limits(limits)
    nonce = secrets.token_hex(8)
    args = json.dumps({
        "content_type": content_type, "language": language, "limits": dataclasses.asdict(limits),
        "root": _REPO_ROOT, "nproc_limit": int(os.environ.get("KB_PARSE_NPROC_LIMIT", "1")),
    })
    cwd = tempfile.mkdtemp(prefix="kbparse-")
    try:
        if _prepare:
            _prepare(cwd)
        proc = await spawn(_child_tail or [_WORKER, args], cwd, nonce)
        return await _supervise(proc, raw, limits, nonce)
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


async def _supervise(proc: asyncio.subprocess.Process, raw: bytes, limits: Limits, nonce: str) -> ParsedDocument:
    pid = proc.pid
    breach: list[str] = []
    cap = limits.max_chars * 4 + (4 << 20)

    async def watchdog() -> None:
        while True:
            await asyncio.sleep(_RSS_POLL_S)
            rss = await _group_rss_mb(pid)
            if rss is not None and rss > limits.memory_mb:
                breach.append("memory")
                _killpg(pid)
                return

    async def stdout() -> tuple[bytes, bool]:
        data, over = await _read_capped(proc.stdout, cap)
        if over:
            breach.append("output")
            _killpg(pid)
        return data, over

    dog = asyncio.create_task(watchdog())
    out_task = asyncio.create_task(stdout())
    err_task = asyncio.create_task(_read_tail(proc.stderr))
    feed_task = asyncio.create_task(_feed(proc, raw))
    out, tail = b"", b""
    try:
        try:
            await asyncio.wait_for(_exited(proc), timeout=limits.timeout_s)
        except asyncio.TimeoutError:
            breach.append("timeout")
        _killpg(pid)  # always: an OCR grandchild must not outlive the parse, and it may hold our pipes open
        try:
            (out, _), tail, _ = await asyncio.wait_for(asyncio.gather(out_task, err_task, feed_task), timeout=10)
        except asyncio.TimeoutError:
            if not breach:
                breach.append("timeout")
    except BaseException:
        _killpg(pid)
        for t in (out_task, err_task, feed_task):
            t.cancel()
        await proc.wait()
        raise
    finally:
        dog.cancel()
        _killpg(pid)
    await proc.wait()
    rc = proc.returncode
    if breach:
        _log_stderr(rc, tail, False, nonce)
        raise ParseRejected({
            "timeout": f"parsing timed out after {limits.timeout_s:g}s (file too large or complex)",
            "memory": "parsing used more memory than allowed (file too large or complex)",
            "output": "parser produced more output than allowed (file too large or complex)",
        }[breach[0]])
    try:
        result = json.loads(out)
    except ValueError:
        result = None
    _log_stderr(rc, tail, result is None, nonce)
    if result is None:
        if rc == EXIT_MEMORY:
            raise ParseRejected("parsing used more memory than allowed (file too large or complex)")
        if rc is not None and rc < 0 and signal.Signals(-rc) in _CPU_SIGNALS:
            raise ParseRejected("parsing used more CPU time than allowed (file too large or complex)")
        # Killed by something else, import failure, host OOM: not the document's fault, so retryable.
        raise ParseInfrastructureError(f"document parser failed to run (exit {rc})")
    if not result.get("ok"):
        raise ParseRejected(result.get("reason") or "file could not be read")
    return ParsedDocument.from_json(result["doc"])
