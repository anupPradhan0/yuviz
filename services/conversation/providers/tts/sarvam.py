"""
SarvamTTS — cloud synthesis via Sarvam AI's Bulbul text-to-speech (POST /text-to-speech).

Built for Indian languages: one voice reads Hindi, Indian English and code-mixed Hinglish
naturally. The REST endpoint returns whole clips (base64 WAV), so synthesize_stream yields
one chunk per sentence, like ElevenLabs.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import math
import wave
from collections import OrderedDict
from typing import Any

import httpx
import numpy as np

from libs.config_sdk.languages import normalize_language

from ..interfaces import INSTANCE_LANGUAGE

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.sarvam.ai"
# Sarvam's recommended model for new integrations. Its speakers are personas named
# <name>_<language>_<style>; the "enhi" ones are built for code-mixed Hindi and English.
DEFAULT_MODEL = "bulbul:v4-flash"
DEFAULT_SPEAKER = "shubh_enhi_ads"
# Registry language -> Sarvam language_code. Only the registry languages Sarvam speaks.
LANGUAGE_CODES = {"en": "en-IN", "hi": "hi-IN"}
# Rates every Bulbul model serves; anything else is requested at the next one up and resampled.
_SUPPORTED_RATES = (8000, 16000, 22050, 24000)
_MAX_CHARS = 2500
# Short phrases recur on every call (tool fillers, the greeting, "anything else?") and a REST
# round trip costs 0.3-1 s each time; their audio is kept, per voice instance, in a small LRU.
_CACHE_MAX_CHARS = 120
_CACHE_ENTRIES = 256
# The pipeline's output rate (PipelineConfig.sample_rate); what prewarm synthesizes at.
_PREWARM_RATE = 16_000


def _nearest_supported_rate(requested: int) -> int:
    for rate in _SUPPORTED_RATES:
        if rate >= requested:
            return rate
    return _SUPPORTED_RATES[-1]


def _pcm_from_wav(data: bytes) -> tuple[bytes, int]:
    """(16-bit mono PCM, sample rate) from a WAV clip."""
    with wave.open(io.BytesIO(data)) as clip:
        rate, channels, width = clip.getframerate(), clip.getnchannels(), clip.getsampwidth()
        frames = clip.readframes(clip.getnframes())
    if width != 2:
        raise ValueError(f"unexpected sample width {width}")
    if channels > 1:
        samples = np.frombuffer(frames, dtype=np.int16).reshape(-1, channels).mean(axis=1)
        frames = samples.astype(np.int16).tobytes()
    return frames, rate


class SarvamTTS:
    """ITTS backed by Sarvam's /text-to-speech.

    language — Sarvam language code ("hi-IN", "en-IN") or a registry code; a per-call
               `language=` overrides it (accepts_language). Hindi is the default, since a
               Hindi request also reads the English words of a Hinglish sentence correctly.
    """

    accepts_language = True

    def __init__(
        self,
        api_key:   str,
        speaker:   str = DEFAULT_SPEAKER,
        model:     str = DEFAULT_MODEL,
        base_url:  str = _DEFAULT_BASE_URL,
        timeout_s: float = 15.0,
        pace:      float = 1.0,
        language:  str | None = None,
    ) -> None:
        self._speaker = speaker
        self._model = model
        self._pace = pace
        self._language = language
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"api-subscription-key": api_key},
            timeout=timeout_s,
        )
        self._cache: OrderedDict[tuple[str, str, int], bytes] = OrderedDict()
        self._prewarmed: set[str] = set()
        log.info("SarvamTTS speaker=%s model=%s pace=%s language=%s", speaker, model, pace, language)

    def _language_code(self, language: Any) -> str:
        chosen = self._language if language is INSTANCE_LANGUAGE else language
        if isinstance(chosen, str) and chosen in LANGUAGE_CODES.values():
            return chosen
        return LANGUAGE_CODES.get(normalize_language(chosen) or "", LANGUAGE_CODES["hi"])

    async def synthesize(self, text: str, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE) -> bytes:
        if not text.strip():
            return b""
        language_code = self._language_code(language)
        key = (text, language_code, sample_rate)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        audio = await self._request(text, sample_rate, language_code)
        if audio and len(text) <= _CACHE_MAX_CHARS:
            self._cache[key] = audio
            if len(self._cache) > _CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return audio

    async def prewarm(self, languages: list[str]) -> None:
        """Synthesizes the filler phrases for each language into the cache (turn-1 and tool-call
        fillers share one pool), once per voice instance, so "one moment" plays without a round trip."""
        from ...i18n import tool_fillers

        for language in languages:
            code = self._language_code(language)
            if code in self._prewarmed:
                continue
            self._prewarmed.add(code)
            phrases = {phrase for phrase, _seconds in tool_fillers(language)}
            await asyncio.gather(*(self.synthesize(p, _PREWARM_RATE, language=language) for p in phrases))

    async def _request(self, text: str, sample_rate: int, language_code: str) -> bytes:
        request_rate = _nearest_supported_rate(sample_rate)
        body = {
            "text": text[:_MAX_CHARS],
            "language_code": language_code,
            "speaker": self._speaker,
            "model": self._model,
            "speech_sample_rate": request_rate,
            "output_audio_codec": "wav",
        }
        if self._pace != 1.0:
            body["pace"] = self._pace
        try:
            resp = await self._client.post("/text-to-speech", json=body)
            resp.raise_for_status()
            clip = base64.b64decode(resp.json()["audios"][0])
            pcm, rate = _pcm_from_wav(clip)
        except httpx.HTTPStatusError as exc:
            # Sarvam's error body names the problem (bad speaker, quota) and holds no secrets.
            log.error("SarvamTTS request failed: %s %s", exc.response.status_code, exc.response.text[:300])
            return b""
        except (httpx.HTTPError, KeyError, IndexError, ValueError, wave.Error):
            log.exception("SarvamTTS request failed")
            return b""
        return pcm if rate == sample_rate else self._resample(pcm, rate, sample_rate)

    async def synthesize_stream(self, text: str, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE):
        audio = await self.synthesize(text, sample_rate, language=language)
        if audio:
            yield audio

    @staticmethod
    def _resample(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
        from scipy.signal import resample_poly

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        gcd = math.gcd(from_rate, to_rate)
        resampled = resample_poly(audio, to_rate // gcd, from_rate // gcd).astype(np.float32)
        return np.clip(resampled * 32767, -32768, 32767).astype(np.int16).tobytes()

    async def aclose(self) -> None:
        await self._client.aclose()
