"""Entry point of the parse subprocess: `python -m services.knowledge.parsers._worker '<json args>'`.
Reads the document bytes on stdin, writes one JSON object on stdout. Never imported by the service."""

from __future__ import annotations

import json
import sys


def _apply_rlimits(memory_mb: int, cpu_s: int) -> None:
    try:
        import resource
    except ImportError:  # Windows
        return
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))
    except (ValueError, OSError):
        pass
    # RLIMIT_AS is honoured on Linux; macOS accepts it but breaks the interpreter's address-space reservations,
    # so there the wall-clock timeout, the byte limit and the bounded inflate in guard_zip() are the caps.
    if sys.platform.startswith("linux"):
        try:
            limit = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (ValueError, OSError):
            pass


def main() -> int:
    args = json.loads(sys.argv[1])
    _apply_rlimits(args["limits"]["memory_mb"], int(args["limits"]["timeout_s"]) + 1)
    raw = sys.stdin.buffer.read()
    from .formats import parse_by_type
    from .limits import Limits
    from .model import ParseRejected

    try:
        doc = parse_by_type(raw, args["content_type"], Limits(**args["limits"]), args.get("language"))
        out = {"ok": True, "doc": doc.to_json()}
    except ParseRejected as exc:
        out = {"ok": False, "reason": exc.reason}
    except MemoryError:
        out = {"ok": False, "reason": "file is too large or complex to read safely"}
    except Exception as exc:  # class name only: the message can carry content
        out = {"ok": False, "reason": f"file could not be read ({type(exc).__name__})"}
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
