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


def harden_privileges() -> None:
    """no_new_privs, non-dumpable and the user/network namespace (Linux). MUST run before any thread exists:
    unshare(CLONE_NEWUSER) fails with EINVAL in a multithreaded process (CI logged errno 22 on every parse when the
    memory-guard thread was started first). Nothing else here assumes a single thread, but order it first anyway."""
    if not sys.platform.startswith("linux"):
        _note("no_new_privs, nproc, RLIMIT_AS and the network namespace are Linux-only; "
              "the parent's RSS watchdog is the memory cap on this platform")
        return
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


def harden_limits(memory_mb: int, cpu_s: int, nproc_limit: int) -> None:
    """rlimits. Runs AFTER the memory-guard thread has started: RLIMIT_NPROC=1 would forbid creating it."""
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
        return
    limit("RLIMIT_AS", memory_mb * 2 * 1024 * 1024)  # virtual size runs well above RSS; the parent enforces RSS
    # Fork is only needed to run the OCR binary; without it the parser has no reason to start processes.
    # RLIMIT_NPROC is per user, so a limit of 1 makes every fork by this process fail.
    if nproc_limit and shutil.which("tesseract") is None:
        limit("RLIMIT_NPROC", nproc_limit)


def harden(memory_mb: int, cpu_s: int, nproc_limit: int) -> None:
    """Both steps in the safe order, for callers that start no thread of their own."""
    harden_privileges()
    harden_limits(memory_mb, cpu_s, nproc_limit)


EXIT_MEMORY = 75


def block_native_xml() -> None:
    """No native XML library at all with KNOWLEDGE_BINARY_PARSERS=0: bs4 imports lxml at import time unless the import
    is refused, so refuse it (a None entry makes `import lxml` raise ImportError, which bs4 already tolerates)."""
    for name in ("lxml", "lxml.etree", "lxml.html", "lxml.objectify"):
        sys.modules[name] = None


# Native thread pools (OpenBLAS inside numpy, OpenMP, MKL) start their worker threads when the library loads. This
# child runs with RLIMIT_NPROC=1 on Linux, so that start fails, and numpy's OpenBLAS answers by killing the process with
# SIGINT (found by the first real GitHub CI run: openpyxl does `import numpy` when numpy is installed, and numpy is
# installed because the platform's requirements pull it in via torch; every XLSX parse died with exit -2).
_THREAD_POOL_ENV = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                    "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS")


def limit_native_threads() -> None:
    """The parsers do no numeric work: refuse numpy outright (openpyxl, Pillow and others treat a missing numpy as
    normal) and cap every native thread pool at one thread in case something else loads one. Must run before any
    library import; the environment is read when the library loads."""
    for name in _THREAD_POOL_ENV:
        os.environ[name] = "1"
    sys.modules["numpy"] = None  # `import numpy` raises ImportError, which every optional-numpy caller already handles


def rss_mb_from_statm(statm: str, page_size: int) -> float:
    """Resident set from /proc/<pid>/statm ("size resident shared ..." in pages), in MB."""
    return int(statm.split()[1]) * page_size / (1024 * 1024)


def usage_mb(platform: str, read_statm, page_size: int, maxrss: int) -> float:
    """Memory in use by THIS process now, in MB.
    Linux: the CURRENT resident set from /proc/self/statm. ru_maxrss must not be used there: it is a high-water mark that
    Linux carries over fork+exec, so a parser child started by a worker whose own peak was 900 MB reports 900 MB from its
    first instruction (CI: exit code 75 for every document, even a 900-byte one; in production, every parse for every
    tenant after the worker's peak crossed the limit). Elsewhere (macOS: bytes, the child's own) ru_maxrss is used."""
    if platform.startswith("linux"):
        try:
            return rss_mb_from_statm(read_statm(), page_size)
        except (OSError, ValueError, IndexError):
            pass  # no /proc: fall through to the high-water mark; memory_exceeded() still subtracts the baseline
    scale = 1024 * 1024 if platform == "darwin" else 1024
    return maxrss / scale


def memory_exceeded(usage: float, baseline: float, limit_mb: float) -> bool:
    """Over the limit AND grown since this process started. The baseline (measured at start) means an inherited high
    value alone can never trigger the exit; real growth still does."""
    return usage > limit_mb and usage - baseline > 0.1 * limit_mb


def _start_memory_guard(limit_mb: int) -> None:
    """Exit the moment this process's memory passes the limit. The parent's watchdog polls too, but this reacts in
    milliseconds and cannot be outrun by a fast allocator. (A long C call that holds the GIL delays it; the parent's
    group-wide watchdog still covers that.) Starts a thread, so it must come after harden_privileges()."""
    import threading
    import time
    try:
        import resource
    except ImportError:
        return
    page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

    def read_statm() -> str:
        with open("/proc/self/statm") as f:
            return f.read()

    def now() -> float:
        return usage_mb(sys.platform, read_statm, page_size, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

    baseline = now()

    def watch() -> None:
        while True:
            time.sleep(0.005)
            if memory_exceeded(now(), baseline, limit_mb):
                os._exit(EXIT_MEMORY)

    threading.Thread(target=watch, daemon=True).start()


def main() -> int:
    args = json.loads(sys.argv[1])
    limit_native_threads()  # before anything imports openpyxl/pptx/Pillow, which may pull in numpy
    # Order matters: unshare(CLONE_NEWUSER) needs a single-threaded process, and RLIMIT_NPROC would forbid a new thread.
    harden_privileges()
    _start_memory_guard(args["limits"]["memory_mb"])
    harden_limits(args["limits"]["memory_mb"], int(args["limits"]["timeout_s"]) + 1, int(args.get("nproc_limit", 1)))
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
