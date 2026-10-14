"""Process-wide model downloads, so a tenant's provider rows cannot multiply them.

Instances are built per tenant-chosen settings (device, compute_type, speed, voice), but a download
is keyed on what it fetches: every instance needing the same files shares one subprocess, at most
MAX_CONCURRENT_DOWNLOADS run at once (the rest wait), and files fetched once are never fetched again
by this process. Subprocesses, unlike executor threads, can be killed: stop_downloads() at shutdown.
"""

from __future__ import annotations

import asyncio
import sys

MAX_CONCURRENT_DOWNLOADS = 2

_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
_in_flight: dict[tuple[str, str], asyncio.Task] = {}
_done: set[tuple[str, str]] = set()


async def fetch(key: tuple[str, str], script: str, *args: str) -> None:
    """Run `python -c script *args` once per key; later and concurrent callers share the result."""
    if key in _done:
        return
    task = _in_flight.get(key)
    if task is None:
        task = _in_flight[key] = asyncio.create_task(_run(key, script, args))
    try:
        await asyncio.shield(task)  # a caller that is cancelled leaves the download for the others
    except asyncio.CancelledError:
        if task.cancelled():  # stop_downloads(), not this caller
            raise RuntimeError("model download stopped") from None
        raise


async def _run(key: tuple[str, str], script: str, args: tuple[str, ...]) -> None:
    try:
        async with _slots:
            proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script, *args)
            try:
                code = await proc.wait()
            finally:
                if proc.returncode is None:  # cancelled at shutdown
                    proc.kill()
                    await proc.wait()
        if code:
            raise RuntimeError(f"model download exited {code}")
        _done.add(key)
    finally:
        del _in_flight[key]


async def stop_downloads() -> None:
    """At shutdown: kill every download in flight."""
    tasks = list(_in_flight.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
