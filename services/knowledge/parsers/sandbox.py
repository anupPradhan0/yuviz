"""Run a parse in a child Python process: a hostile file can hang, exhaust memory or crash a C extension, and
none of that may take the ingestion worker down with it.

WHAT THIS IS: a resource-limited, isolated-interpreter child. It is NOT a sandbox against a compromised parser.
A document that exploits lxml, Pillow or pypdf gets code execution as the SAME OS USER as the worker, so the
in-process steps below only raise the bar. Full containment needs a separate uid or container with a read-only
filesystem, no network and none of the service's secrets; see docs/knowledge-parser-sandbox.md.

What the child gets (details and measured limits: docs/knowledge-parser-sandbox.md):
  - `python -I`; cwd = a fresh empty temp dir, removed afterwards; close_fds; its own session and process group
  - an environment of PATH, LANG and a per-run log nonce (not the parent's environment variables)
  - wall-clock timeout, output byte cap, and two memory guards that work on macOS and Linux: the parent polls the RSS of the
    whole process group every 20 ms, and the child exits itself when its own peak RSS passes the limit; any breach
    SIGKILLs the process group
  - in the child, Linux only and best effort (see _worker.harden): rlimits, no_new_privs, non-dumpable, and an attempt at
    unshare(CLONE_NEWUSER|CLONE_NEWNET) then CLONE_NEWNET to drop the network
NOT covered, measured on macOS and reasoned for Linux: the child runs as the same user, so it can read every file that user can
(all tenants' stored documents, .env files), reach the database and the internet wherever the namespace unshare is refused
(which is most deployments), and read the environment of any same-user process: the worker's only on Linux (PR_SET_DUMPABLE,
hardening.py), the API's never, and on macOS the parent's too. A child can also escape the group kill with fork()+setsid().
Do not enable PDF/Office parsing for untrusted tenants without the separate-container deployment; KNOWLEDGE_BINARY_PARSERS=0
turns it off."""

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
_RSS_POLL_MIN_S = 0.02          # while memory is near the limit or growing quickly
_RSS_POLL_MAX_S = 0.5           # when it is small and stable (spawning `ps` 50x a second cost ~35% of a core per parse)
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


def _group_rss_proc(pgid: int) -> float:
    """RSS of every process in the group straight from /proc (Linux): no process is spawned."""
    page = os.sysconf("SC_PAGE_SIZE")
    total = 0
    with os.scandir("/proc") as it:
        for e in it:
            if not e.name.isdigit():
                continue
            try:
                with open(f"/proc/{e.name}/stat", "rb") as f:
                    data = f.read()
            except OSError:
                continue
            # pid (comm) state ppid pgrp ... rss: comm may hold spaces and parentheses, so split after the last ')'
            fields = data[data.rfind(b")") + 2:].split()
            if len(fields) > 21 and int(fields[2]) == pgid:
                total += int(fields[21]) * page
    return total / (1024 * 1024)


async def _group_rss_mb(pgid: int) -> float | None:
    """Resident memory of the whole process group (the parser and anything it started), in MB. /proc on Linux, otherwise
    `ps -axo pgid=,rss=` (the same on macOS and Linux)."""
    if sys.platform.startswith("linux") and os.path.isdir("/proc"):
        try:
            return await asyncio.to_thread(_group_rss_proc, pgid)
        except (OSError, ValueError):
            pass
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


def _next_poll_interval(interval: float, rss: float, last: float, limit_mb: float) -> float:
    """Poll fast while memory is large (over 40% of the limit) or growing quickly (over 5% of the limit since the last
    poll); otherwise back off, doubling up to the maximum."""
    if rss > 0.4 * limit_mb or rss - last > 0.05 * limit_mb:
        return _RSS_POLL_MIN_S
    return min(_RSS_POLL_MAX_S, interval * 2)


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
        # Fast while memory is near the limit or growing quickly, slow while it is small and flat; the child's own guard
        # (a few ms) is the fast backstop either way.
        interval, last = _RSS_POLL_MIN_S, 0.0
        while True:
            await asyncio.sleep(interval)
            rss = await _group_rss_mb(pid)
            if rss is None:
                interval = _RSS_POLL_MAX_S
                continue
            if rss > limits.memory_mb:
                breach.append("memory")
                _killpg(pid)
                return
            interval = _next_poll_interval(interval, rss, last, limits.memory_mb)
            last = rss

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
