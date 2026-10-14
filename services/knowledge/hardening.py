"""Process hardening for the ingestion worker. The worker holds the DB DSN and other secrets in its environment and
spawns document parsers as the same OS user; making it non-dumpable stops those children reading
/proc/<worker>/environ or /proc/<worker>/mem. Linux only; elsewhere it logs that nothing was done. It protects the
worker only: the Knowledge API and other same-user processes stay readable, and so does everything on macOS."""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


def harden_worker_process() -> bool:
    """PR_SET_DUMPABLE=0. Returns True when applied."""
    if not sys.platform.startswith("linux"):
        log.warning("worker hardening skipped: PR_SET_DUMPABLE is Linux-only (platform=%s)", sys.platform)
        return False
    try:
        import ctypes
        if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) == 0:  # PR_SET_DUMPABLE
            return True
        log.warning("worker hardening failed: prctl(PR_SET_DUMPABLE) errno=%s", ctypes.get_errno())
    except Exception as exc:
        log.warning("worker hardening failed: %s", type(exc).__name__)
    return False
