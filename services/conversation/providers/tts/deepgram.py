"""
DeepgramTTS — cloud synthesis via Deepgram's Aura text-to-speech endpoint.

Unlike ElevenLabs, Deepgram's /v1/speak accepts an arbitrary linear16 sample
rate directly (encoding=linear16&sample_rate=<rate>&container=none) — no
fixed-rate-then-resample dance needed here.

pip install httpx (already a dependency via OllamaLLM)
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

import httpx

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.deepgram.com"


class DeepgramTTS:
    """
    ITTS implementation backed by Deepgram's /v1/speak (Aura).

    api_key — resolved once at construction by AIProviderManager via
              SecretResolver, never re-resolved per call.
    voice   — an Aura model name, e.g. "aura-asteria-en".
    """

    def __init__(
        self,
        api_key:   str,
        voice:     str = "aura-asteria-en",
        base_url:  str = _DEFAULT_BASE_URL,
        timeout_s: float = 15.0,
    ) -> None:
        self._voice = voice
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Token {api_key}"},
            timeout=timeout_s,
        )
        log.info("DeepgramTTS voice=%s", voice)

    async def synthesize(self, text: str, sample_rate: int) -> bytes:
        if not text.strip():
            return b""

        try:
            resp = await self._client.post(
                "/v1/speak",
                params={
                    "model": self._voice,
                    "encoding": "linear16",
                    "sample_rate": sample_rate,
                    "container": "none",
                },
                json={"text": text},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            log.exception("DeepgramTTS request failed")
            return b""

        return resp.content

    async def synthesize_stream(self, text: str, sample_rate: int) -> AsyncGenerator[bytes, None]:
        # Confirmed live 2026-08-01: Deepgram's /v1/speak response body
        # arrives progressively (first byte ~800ms, last byte ~1600ms for a
        # single sentence) — client.stream()+aiter_bytes() forwards each
        # chunk the moment it lands instead of blocking on resp.content
        # until the whole utterance has downloaded. This is the entire
        # latency win: same API, same request, just not throwing away the
        # server's own streaming behavior.
        if not text.strip():
            return

        try:
            async with self._client.stream(
                "POST", "/v1/speak",
                params={
                    "model": self._voice,
                    "encoding": "linear16",
                    "sample_rate": sample_rate,
                    "container": "none",
                },
                json={"text": text},
            ) as resp:
                resp.raise_for_status()
                # aiter_bytes() yields at arbitrary HTTP chunk boundaries, not
                # 16-bit-sample boundaries — an odd-length chunk here becomes
                # an invalid Int16Array downstream (browser/Gateway both treat
                # this as PCM16). Carry any trailing odd byte over to the next
                # chunk instead of yielding misaligned bytes.
                pending = b""
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    data = pending + chunk
                    even_len = len(data) - (len(data) % 2)
                    pending = data[even_len:]
                    if even_len:
                        yield data[:even_len]
        except httpx.HTTPError:
            log.exception("DeepgramTTS streaming request failed")

    async def aclose(self) -> None:
        await self._client.aclose()
