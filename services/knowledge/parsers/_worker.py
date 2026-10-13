"""Entry point of the parse subprocess, run as `python -I _worker.py '<json args>'` (see sandbox.py).
Reads the document bytes on stdin, writes one JSON object on stdout. Never imported by the service.

-I (isolated mode) keeps the working directory, the script directory, PYTHONPATH and user site-packages off
sys.path, so a planted json.py or lxml.py cannot be imported; the repo root is appended AFTER the standard
library and site-packages for the same reason.

Hardening below is best effort and Linux-specific where noted. It narrows what a compromised parser can do; it is
not a sandbox (see docs/knowledge-parser-sandbox.md). Every step that fails is reported on stderr as a
`hardening:` line, which the parent logs, and the parse continues."""

from __future__ import annotations

import json
import os
import shutil
import sys


def _note(msg: str) -> None:
    # Tagged with the parent's per-run nonce so the parent can tell these from document text on stderr.
    sys.stderr.write(f"hardening[{os.environ.get('KB_PARSE_NONCE', '')}]: {msg}\n")
    sys.stderr.flush()


def _libc():
    import ctypes
    return ctypes.CDLL(None, use_errno=True)


def harden(memory_mb: int, cpu_s: int, nproc_limit: int) -> None:
    try:
        import resource
    except ImportError:  # Windows
        _note("resource module unavailable; no rlimits applied")
        return

    def limit(name: str, soft: int, hard: int | None = None) -> None:
        res = getattr(resource, name, None)
        if res is None:
            return
        try:
            cur_soft, cur_hard = resource.getrlimit(res)
            hard = soft if hard is None else hard
            if cur_hard != resource.RLIM_INFINITY:
                soft, hard = min(soft, cur_hard), min(hard, cur_hard)
            resource.setrlimit(res, (soft, hard))
        except (ValueError, OSError) as exc:
            _note(f"{name} not applied: {exc}")

    limit("RLIMIT_CPU", cpu_s, cpu_s + 1)
    limit("RLIMIT_FSIZE", 0)       # the parser never writes files (stdout is a pipe, unaffected)
    limit("RLIMIT_CORE", 0)
    limit("RLIMIT_NOFILE", 64)
    if not sys.platform.startswith("linux"):
        _note("memory cap (RLIMIT_AS), no_new_privs, nproc and network namespace are Linux-only; "
              "the parent's RSS watchdog is the memory cap on this platform")
        return
    limit("RLIMIT_AS", memory_mb * 2 * 1024 * 1024)  # virtual size runs well above RSS; the parent enforces RSS
    # Fork is only needed to run the OCR binary; without it the parser has no reason to start processes.
    # RLIMIT_NPROC is per user, so a limit of 1 makes every fork by this process fail.
    if nproc_limit and shutil.which("tesseract") is None:
        limit("RLIMIT_NPROC", nproc_limit)
    try:
        libc = _libc()
        if libc.prctl(38, 1, 0, 0, 0) != 0:         # PR_SET_NO_NEW_PRIVS
            _note("PR_SET_NO_NEW_PRIVS failed")
        if libc.prctl(4, 0, 0, 0, 0) != 0:          # PR_SET_DUMPABLE = 0: same-uid processes cannot read /proc/<pid>
            _note("PR_SET_DUMPABLE failed")
        import ctypes
        # An unprivileged user namespace first (CLONE_NEWUSER|CLONE_NEWNET needs no capability where the kernel allows
        # it); then plain CLONE_NEWNET (needs CAP_SYS_ADMIN, so it mostly fails); else the child keeps the network.
        if libc.unshare(0x10000000 | 0x40000000) != 0:
            first = ctypes.get_errno()
            if libc.unshare(0x40000000) != 0:
                _note(f"network namespace unavailable (errno {first} with a user namespace, {ctypes.get_errno()} without); "
                      "child keeps network access")
    except Exception as exc:  # no libc, ctypes blocked, seccomp
        _note(f"prctl/unshare not applied: {type(exc).__name__}")


EXIT_MEMORY = 75


def block_native_xml() -> None:
    """No native XML library at all with KNOWLEDGE_BINARY_PARSERS=0: bs4 imports lxml at import time unless the import
    is refused, so refuse it (a None entry makes `import lxml` raise ImportError, which bs4 already tolerates)."""
    for name in ("lxml", "lxml.etree", "lxml.html", "lxml.objectify"):
        sys.modules[name] = None


def _start_memory_guard(limit_mb: int) -> None:
    """Exit the moment this process's peak RSS passes the limit. The parent's watchdog polls too, but this reacts in
    milliseconds and cannot be outrun by a fast allocator. (A long C call that holds the GIL delays it; the parent's
    group-wide watchdog still covers that.)"""
    import threading
    import time
    try:
        import resource
    except ImportError:
        return
    scale = 1024 * 1024 if sys.platform == "darwin" else 1024     # ru_maxrss is bytes on macOS, KB on Linux

    def watch() -> None:
        while True:
            time.sleep(0.005)
            if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale > limit_mb:
                os._exit(EXIT_MEMORY)

    threading.Thread(target=watch, daemon=True).start()


def main() -> int:
    args = json.loads(sys.argv[1])
    _start_memory_guard(args["limits"]["memory_mb"])   # before harden(): RLIMIT_NPROC would forbid starting a thread
    harden(args["limits"]["memory_mb"], int(args["limits"]["timeout_s"]) + 1, int(args.get("nproc_limit", 1)))
    raw = sys.stdin.buffer.read()
    sys.path.append(args["root"])
    if not args["limits"].get("binary_parsers", True):
        block_native_xml()
    try:
        from services.knowledge.parsers.formats import parse_by_type
        from services.knowledge.parsers.limits import Limits
        from services.knowledge.parsers.model import ParseRejected
    except Exception as exc:
        sys.stderr.write(f"ImportError: parser modules failed to import ({type(exc).__name__})\n")
        return 3
    try:
        doc = parse_by_type(raw, args["content_type"], Limits(**args["limits"]), args.get("language"))
        payload = doc.to_json()
        payload["page_texts"] = None   # only the quality gate needs these, and it already ran here
        out = {"ok": True, "doc": payload}
    except ParseRejected as exc:
        out = {"ok": False, "reason": exc.reason}
    except MemoryError:
        out = {"ok": False, "reason": "file is too large or complex to read safely"}
    except Exception as exc:  # class name only: the message can carry content
        out = {"ok": False, "reason": f"file could not be read ({type(exc).__name__})"}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
