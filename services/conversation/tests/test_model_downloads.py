"""Model downloads are shared per file set and capped per process, whatever the tenants' rows say."""

from __future__ import annotations

import asyncio

import pytest

from .. import model_downloads


class _Proc:
    """A download that ends only when `release` is set (or it is killed)."""

    def __init__(self, release: asyncio.Event, code: int = 0) -> None:
        self.returncode = None
        self._release, self._code = release, code

    async def wait(self):
        await self._release.wait()
        self.returncode = self._code if self.returncode is None else self.returncode
        return self.returncode

    def kill(self):
        self.returncode = -9
        self._release.set()


@pytest.fixture
def spawner(monkeypatch):
    monkeypatch.setattr(model_downloads, "_in_flight", {})
    monkeypatch.setattr(model_downloads, "_done", set())
    monkeypatch.setattr(model_downloads, "_slots", asyncio.Semaphore(model_downloads.MAX_CONCURRENT_DOWNLOADS))
    release = asyncio.Event()
    spawned: list[tuple] = []

    async def spawn(*argv):
        spawned.append(argv)
        return _Proc(release)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    spawner = type("S", (), {})()
    spawner.release, spawner.spawned = release, spawned
    return spawner


async def test_many_callers_of_one_download_share_one_subprocess(spawner):
    waiters = [asyncio.create_task(model_downloads.fetch(("faster_whisper", "small"), "script", "small")) for _ in range(20)]
    await asyncio.sleep(0.05)
    assert len(spawner.spawned) == 1
    spawner.release.set()
    await asyncio.gather(*waiters)
    await model_downloads.fetch(("faster_whisper", "small"), "script", "small")  # done: no new download
    assert len(spawner.spawned) == 1


async def test_concurrent_downloads_are_capped_and_the_rest_wait(spawner):
    cap = model_downloads.MAX_CONCURRENT_DOWNLOADS
    tasks = [asyncio.create_task(model_downloads.fetch(("faster_whisper", f"m{i}"), "script")) for i in range(cap + 3)]
    await asyncio.sleep(0.05)
    assert len(spawner.spawned) == cap
    spawner.release.set()
    await asyncio.gather(*tasks)
    assert len(spawner.spawned) == cap + 3


async def test_a_failed_download_is_retried_by_a_later_caller(spawner, monkeypatch):
    async def fails(*argv):
        spawner.spawned.append(argv)
        spawner.release.set()
        return _Proc(spawner.release, code=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fails)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await model_downloads.fetch(("faster_whisper", "small"), "script")
    assert len(spawner.spawned) == 2


async def test_stop_downloads_kills_what_is_in_flight(spawner):
    waiter = asyncio.create_task(model_downloads.fetch(("faster_whisper", "small"), "script"))
    await asyncio.sleep(0.05)
    await asyncio.wait_for(model_downloads.stop_downloads(), timeout=2)
    assert spawner.release.is_set()
    waiter.cancel()
