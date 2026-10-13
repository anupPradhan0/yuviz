"""A fake embedding server over real sockets (HTTP/1.1 keep-alive), for the embed-timeout tests and benchmark.

Speaks the OpenAI shape (POST /v1/embeddings), the Ollama shape (POST /api/embeddings) and HEAD /, returns the
deterministic bag-of-words vectors of rag_fakes (so dense hits are real), and counts accepted connections and
requests. `latency_s` is added to every embedding request; `connect_delay_s` is slept once per new connection before
it is served, which models a TLS handshake (plain HTTP here: the delay is an assumption, not a measurement of TLS)."""

from __future__ import annotations

import asyncio
import json

from rag_fakes import embed_text


class FakeEmbedServer:
    def __init__(self, latency_s: float = 0.0, connect_delay_s: float = 0.0) -> None:
        self.latency_s, self.connect_delay_s = latency_s, connect_delay_s
        self.connections = 0
        self.requests = 0
        self.heads = 0
        self.head_headers: list[dict] = []
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> "FakeEmbedServer":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def openai_base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def ollama_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        if self.connect_delay_s:
            await asyncio.sleep(self.connect_delay_s)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                method, path = lines[0].split(" ")[:2]
                headers = {k.lower(): v for k, v in (ln.split(": ", 1) for ln in lines[1:] if ": " in ln)}
                body = await reader.readexactly(int(headers.get("content-length", "0")))
                if method == "HEAD":
                    self.heads += 1
                    self.head_headers.append(headers)
                    payload = b""
                else:
                    self.requests += 1
                    if self.latency_s:
                        await asyncio.sleep(self.latency_s)
                    req = json.loads(body)
                    if path.endswith("/embeddings") and "input" in req:
                        texts = req["input"] if isinstance(req["input"], list) else [req["input"]]
                        payload = json.dumps({"data": [{"index": i, "embedding": embed_text(t)}
                                                       for i, t in enumerate(texts)]}).encode()
                    else:
                        payload = json.dumps({"embedding": embed_text(req.get("prompt", ""))}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: keep-alive\r\n"
                             + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
