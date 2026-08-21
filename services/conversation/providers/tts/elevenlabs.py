"""
ElevenLabsTTS — cloud synthesis via ElevenLabs' text-to-speech endpoint.

ElevenLabs only serves a fixed set of PCM sample rates (8000/16000/22050/
24000/44100), not arbitrary ones — request the nearest rate that's >= the
caller's requested rate, then resample down if it doesn't match exactly
(same resample_poly approach as MacOSTTS, needed there for the same reason:
the source audio's native rate rarely equals the gateway's requested rate).

pip install httpx (already a dependency via OllamaLLM)
"""

from __future__ import annotations

import logging

import httpx
import numpy as np

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.elevenlabs.io"

# ElevenLabs' supported PCM output rates, ascending — pick the smallest one
# >= the requested rate so we only ever downsample, never upsample (upsampling
# can't recover detail the source never had).
_SUPPORTED_PCM_RATES = (8000, 16000, 22050, 24000, 44100)


def _nearest_supported_rate(requested: int) -> int:
    for rate in _SUPPORTED_PCM_RATES:
        if rate >= requested:
            return rate
    return _SUPPORTED_PCM_RATES[-1]


class ElevenLabsTTS:
    """
    ITTS implementation backed by ElevenLabs' /v1/text-to-speech/{voice_id}.

    api_key       — resolved once at construction by AIProviderManager via
                    SecretResolver, never re-resolved per call.
    voice_id      — ElevenLabs voice id (not a display name — see
                    https://elevenlabs.io/app/voice-library for ids).
    model_id      — e.g. "eleven_turbo_v2_5" (lower latency) or "eleven_multilingual_v2"
    language_code — ISO 639-1 code (e.g. "hi", "fr") forcing the output
                    language on a multilingual voice/model, independent of
                    the voice's own "native" language/accent. None = let
                    ElevenLabs auto-detect from the input text, its
                    default behavior. Only meaningful with a multilingual
                    model_id (eleven_turbo_v2_5/eleven_multilingual_v2/
                    eleven_flash_v2_5, all default-capable); ElevenLabs
                    silently ignores it on non-multilingual models rather
                    than erroring, so no validation against model_id here.
    """

    def __init__(
        self,
        api_key:       str,
        voice_id:      str,
        model_id:      str = "eleven_turbo_v2_5",
        base_url:      str = _DEFAULT_BASE_URL,
        timeout_s:     float = 15.0,
        speed:         float = 1.0,
        language_code: str | None = None,
    ) -> None:
        self._voice_id      = voice_id
        self._speed         = speed
        self._model_id      = model_id
        self._language_code = language_code
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"xi-api-key": api_key},
            timeout=timeout_s,
        )
        log.info(
            "ElevenLabsTTS voice_id=%s model_id=%s language_code=%s",
            voice_id, model_id, language_code,
        )

    async def synthesize(self, text: str, sample_rate: int) -> bytes:
        if not text.strip():
            return b""

        output_rate = _nearest_supported_rate(sample_rate)

        try:
            resp = await self._client.post(
                f"/v1/text-to-speech/{self._voice_id}",
                params={"output_format": f"pcm_{output_rate}"},
                json={
                    "text": text,
                    "model_id": self._model_id,
                    # voice_settings only when non-default — identical request
                    # to before for speed=1.0 (ElevenLabs' own default).
                    **(
                        {"voice_settings": {"speed": self._speed}}
                        if self._speed != 1.0 else {}
                    ),
                    **({"language_code": self._language_code} if self._language_code else {}),
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            log.exception("ElevenLabsTTS request failed")
            return b""

        pcm = resp.content
        if output_rate == sample_rate:
            return pcm

        return self._resample(pcm, output_rate, sample_rate)

    async def synthesize_stream(self, text: str, sample_rate: int):
        # No genuine incremental synthesis here — yield the one complete
        # result once. See ITTS.synthesize_stream's docstring: only
        # DeepgramTTS does real chunk-by-chunk streaming today.
        audio = await self.synthesize(text, sample_rate)
        if audio:
            yield audio

    @staticmethod
    def _resample(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
        import math
        from scipy.signal import resample_poly

        audio_i16 = np.frombuffer(pcm, dtype=np.int16)
        audio_f32 = audio_i16.astype(np.float32) / 32768.0

        gcd = math.gcd(from_rate, to_rate)
        resampled = resample_poly(audio_f32, to_rate // gcd, from_rate // gcd).astype(np.float32)

        return np.clip(resampled * 32767, -32768, 32767).astype(np.int16).tobytes()

    async def aclose(self) -> None:
        await self._client.aclose()
