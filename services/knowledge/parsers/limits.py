"""Parse limits. Env-tunable; every one has a plain rejection reason in parse_document()."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 50 * 1024 * 1024
    max_pages: int = 500
    max_decompressed: int = 256 * 1024 * 1024   # zip members, summed, as actually inflated
    max_chars: int = 10_000_000                 # extracted text
    max_table_rows: int = 200_000
    timeout_s: float = 60.0                     # wall clock for the whole parse subprocess
    memory_mb: int = 2048                       # RLIMIT_AS; enforced on Linux only (see sandbox.py)

    @classmethod
    def from_env(cls) -> "Limits":
        d = cls()
        return cls(
            max_bytes=int(_env("KB_PARSE_MAX_BYTES", d.max_bytes)),
            max_pages=int(_env("KB_PARSE_MAX_PAGES", d.max_pages)),
            max_decompressed=int(_env("KB_PARSE_MAX_DECOMPRESSED", d.max_decompressed)),
            max_chars=int(_env("KB_PARSE_MAX_CHARS", d.max_chars)),
            max_table_rows=int(_env("KB_PARSE_MAX_TABLE_ROWS", d.max_table_rows)),
            timeout_s=_env("KB_PARSE_TIMEOUT_S", d.timeout_s),
            memory_mb=int(_env("KB_PARSE_MEMORY_MB", d.memory_mb)),
        )
