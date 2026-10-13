"""Hosted-embedding latency: per-call client (the behaviour before keep-alive), cold persistent client (first call),
warm persistent client, against a fake embedding server over real localhost sockets that adds 100/200/300/400 ms.

    python services/knowledge/tests/bench_embed_hosted.py

`connect_delay` models the TCP+TLS handshake a hosted API costs on every NEW connection (plain HTTP on localhost has
almost none, so the modelled value is an assumption, not a measurement of TLS). Both settings are printed; the
hand-shake column of the 80 ms rows is therefore illustrative, the 0 ms rows are measured as-is."""

from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "..")))
os.environ.setdefault("JWT_SECRET", "bench-only-" * 8)

import httpx  # noqa: E402
from fake_embed_server import FakeEmbedServer  # noqa: E402

from services.knowledge.embedding_manager import EMBEDDING_DIMS, OpenAIEmbeddingProvider  # noqa: E402


class PerCallClientProvider(OpenAIEmbeddingProvider):
    """The old behaviour: a fresh httpx client (and connection) per embed call."""

    async def embed(self, texts):
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{self._base_url}/embeddings", headers={"Authorization": "Bearer k"},
                                     json={"model": self._model, "input": texts, "dimensions": EMBEDDING_DIMS})
            resp.raise_for_status()
            return [d["embedding"] for d in resp.json()["data"]]


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


async def timed(fn):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await fn()
    return (loop.time() - t0) * 1000


async def main() -> None:
    print("budget for hosted engines: 300 ms (a call slower than that falls back to lexical-only)")
    print(f"{'connect model':>13} {'embed latency':>13} | {'mode':26} {'n':>3} {'p50 ms':>8} {'p95 ms':>8} {'within 300 ms':>14}")
    for connect_delay in (0.0, 0.080):
        for latency in (0.100, 0.200, 0.300, 0.400):
            server = await FakeEmbedServer(latency_s=latency, connect_delay_s=connect_delay).start()
            try:
                rows = []
                before = PerCallClientProvider("k", base_url=server.openai_base)
                rows.append(("per-call client (before)", [await timed(lambda: before.embed(["refund question"])) for _ in range(30)]))
                cold = []
                for _ in range(15):  # a fresh provider each time: first call opens the connection
                    p = OpenAIEmbeddingProvider("k", base_url=server.openai_base)
                    cold.append(await timed(lambda: p.embed(["refund question"])))
                    await p.aclose()
                rows.append(("persistent, cold (1st call)", cold))
                warm_p = OpenAIEmbeddingProvider("k", base_url=server.openai_base)
                await warm_p.warm()
                rows.append(("persistent, warm", [await timed(lambda: warm_p.embed(["refund question"])) for _ in range(30)]))
                await warm_p.aclose()
                for name, vals in rows:
                    within = sum(v <= 300 for v in vals) / len(vals)
                    print(f"{connect_delay * 1000:>11.0f}ms {latency * 1000:>11.0f}ms | {name:26} {len(vals):>3} "
                          f"{pct(vals, 50):8.1f} {pct(vals, 95):8.1f} {within:>13.0%}")
            finally:
                await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
