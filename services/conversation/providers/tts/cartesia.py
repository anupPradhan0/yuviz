"""
CartesiaTTS — cloud synthesis via Cartesia's Sonic text-to-speech endpoint.

Shaped after DeepgramTTS deliberately: same two methods, same odd-byte
carry, same "resolve the key once at construction" contract, so the two are
interchangeable behind ITTS and a tenant can switch engine without anything
downstream knowing.

Two differences from Deepgram worth knowing:

  - Cartesia takes its parameters in the JSON BODY, not the query string,
    including the output format. It will return raw PCM at an arbitrary
    sample rate, so like Deepgram there is no fixed-rate-then-resample step.

  - Its streaming endpoint is /tts/sse, which frames audio inside
    Server-Sent Events rather than returning a bare audio stream. That
    framing has to be unwrapped (see synthesize_stream), and measured on
    this network it bought only ~100ms over the plain /tts/bytes endpoint —
    so synthesize() uses the simpler one and is not merely a fallback.

pip install httpx (already a dependency via OllamaLLM)
"""

from __future__ import annotations

import base64
import json
import logging
from typing import AsyncGenerator

import httpx

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.cartesia.ai"
# Cartesia dates its API rather than versioning it by path. Pinned, not read
# from config: a newer date can change the response shape, and that is a
# code change here, not a per-tenant setting.
_API_VERSION = "2024-06-10"
_DEFAULT_MODEL = "sonic-2"


class CartesiaTTS:
    """
    ITTS implementation backed by Cartesia's /tts endpoints.

    api_key — resolved once at construction by AIProviderManager via
              SecretResolver, never re-resolved per call.
    voice   — a Cartesia voice id (a UUID), NOT a human name. The console
              shows names; the id is what the API wants.
    model   — "sonic-2" unless a tenant pins an older one via extra.model.
    """

    def __init__(
        self,
        api_key:   str,
        voice:     str,
        model:     str = _DEFAULT_MODEL,
        base_url:  str = _DEFAULT_BASE_URL,
        timeout_s: float = 15.0,
    ) -> None:
        self._voice = voice
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "X-API-Key": api_key,
                "Cartesia-Version": _API_VERSION,
            },
            timeout=timeout_s,
        )
        log.info("CartesiaTTS voice=%s model=%s", voice, model)

    def _body(self, text: str, sample_rate: int) -> dict:
        return {
            "model_id": self._model,
            "transcript": text,
            "voice": {"mode": "id", "id": self._voice},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": sample_rate,
            },
        }

    async def synthesize(self, text: str, sample_rate: int) -> bytes:
        if not text.strip():
            return b""

        try:
            resp = await self._client.post("/tts/bytes", json=self._body(text, sample_rate))
            resp.raise_for_status()
        except httpx.HTTPError:
            log.exception("CartesiaTTS request failed")
            return b""

        return resp.content

    async def synthesize_stream(self, text: str, sample_rate: int) -> AsyncGenerator[bytes, None]:
        """Unwraps Cartesia's SSE framing into raw PCM.

        Each event is a JSON object; the ones carrying audio have
        type="chunk" and a base64 `data` field. A "done" type ends the
        stream. Anything else (timestamps, errors) is ignored rather than
        treated as audio — feeding a non-audio event to the caller would
        surface as a burst of noise, which is far harder to diagnose than
        silence.
        """
        if not text.strip():
            return

        try:
            async with self._client.stream(
                "POST", "/tts/sse", json=self._body(text, sample_rate),
            ) as resp:
                resp.raise_for_status()
                # Same odd-byte carry as DeepgramTTS: base64 chunks decode to
                # arbitrary byte lengths, not 16-bit-sample boundaries, and an
                # odd-length yield becomes an invalid Int16Array downstream.
                pending = b""
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        log.warning("CartesiaTTS: undecodable SSE payload, skipping")
                        continue
                    if event.get("type") == "done":
                        break
                    encoded = event.get("data")
                    if not encoded:
                        continue
                    try:
                        audio = base64.b64decode(encoded)
                    except (ValueError, TypeError):
                        log.warning("CartesiaTTS: undecodable audio chunk, skipping")
                        continue

                    data = pending + audio
                    even_len = len(data) - (len(data) % 2)
                    pending = data[even_len:]
                    if even_len:
                        yield data[:even_len]
        except httpx.HTTPError:
            log.exception("CartesiaTTS streaming request failed")

    async def aclose(self) -> None:
        await self._client.aclose()
