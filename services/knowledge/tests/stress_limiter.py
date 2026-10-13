"""Standalone limiter stress (stdlib only, so it runs on any Python): python stress_limiter.py [iterations]
Loads resilience.py by path, so it needs none of the service's dependencies. Exit code 1 on any invariant violation."""

import asyncio
import importlib.util
import pathlib
import random
import sys

spec = importlib.util.spec_from_file_location("resilience", pathlib.Path(__file__).resolve().parents[1] / "resilience.py")
res = importlib.util.module_from_spec(spec)
sys.modules["resilience"] = res
spec.loader.exec_module(res)


def idle(lim):
    return lim.in_flight == 0 and lim._per == {} and lim._queues == {} and not lim._order


async def deterministic():
    cfg = res.RetrievalConfig(max_inflight=1, tenant_max_inflight=1)
    lim = res.AdmissionLimiter()
    assert await lim.acquire("a", cfg, 0)  # cancel before hand-over
    w = asyncio.create_task(lim.acquire("b", cfg, 5))
    await asyncio.sleep(0)
    w.cancel()
    try:
        await w
    except asyncio.CancelledError:
        pass
    lim.release("a")
    assert idle(lim)
    assert await lim.acquire("a", cfg, 0)  # cancel after hand-over
    w = asyncio.create_task(lim.acquire("b", cfg, 5))
    await asyncio.sleep(0)
    lim.release("a")
    assert lim.in_flight == 1
    w.cancel()
    try:
        await w
    except asyncio.CancelledError:
        pass
    assert idle(lim)


async def fuzz(iterations, seed):
    rng = random.Random(seed)
    lim = res.AdmissionLimiter()
    for _ in range(iterations):
        cfg = res.RetrievalConfig(max_inflight=rng.randint(1, 4), tenant_max_inflight=rng.randint(1, 3))

        async def worker(key, hold, wait):
            if await lim.acquire(key, cfg, wait):
                try:
                    await asyncio.sleep(hold)
                finally:
                    lim.release(key)

        tasks = [asyncio.create_task(worker(rng.choice("abcd"), rng.choice([0, 0, 0.001, 0.003]),
                                            rng.choice([0, 0.001, 0.004, 0.02]))) for _ in range(rng.randint(2, 10))]
        for t in tasks:
            if rng.random() < 0.5:
                await asyncio.sleep(rng.choice([0, 0, 0.0005, 0.002]))
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert idle(lim), (lim.in_flight, lim._per, lim._queues)


async def main(n):
    await deterministic()
    await fuzz(n, 1234)
    print(f"python {sys.version.split()[0]}: deterministic cancel orders + {n} random interleavings OK")

asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 1000))
