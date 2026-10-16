"""
KokoroTTS — local neural TTS (24 kHz output, resampled to the requested rate).

Requires Python <= 3.12: kokoro's spacy/blis dependency doesn't build on 3.13+.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

import numpy as np
from huggingface_hub import hf_hub_download
from huggingface_hub.errors import LocalEntryNotFoundError

from libs.config_sdk.languages import LANGUAGES, normalize_language

from ... import model_downloads
from ..interfaces import INSTANCE_LANGUAGE

# After a failed fetch, no new attempt for this long: an offline host must not start a
# subprocess on every sentence.
_RETRY_AFTER_S = 60

log = logging.getLogger(__name__)

_KOKORO_NATIVE_RATE = 24_000  # Hz — kokoro's output sample rate

KOKORO_REPO = "hexgrad/Kokoro-82M"

_DOWNLOAD_SCRIPT = (
    "import sys; from huggingface_hub import hf_hub_download; "
    "[hf_hub_download(sys.argv[1], name) for name in sys.argv[2:]]"
)


def kokoro_lang_code(language: str | None) -> str | None:
    """ISO 639-1 -> KPipeline lang_code (en->a, hi->h, ...); None when Kokoro has no voice for it."""
    lang = LANGUAGES.get(normalize_language(language) or "")
    return lang.kokoro_code if lang else None


def _row_language(lang_code: str) -> str | None:
    """ISO 639-1 language a KPipeline lang_code speaks ('a'/'b' are both English)."""
    if lang_code in ("a", "b"):
        return "en"
    return next((iso for iso, lang in LANGUAGES.items() if lang.kokoro_code == lang_code), None)


class KokoroTTS:
    """
    ITTS implementation backed by kokoro (KPipeline).

    voice       — kokoro voice name (e.g. "af_sarah", "am_adam", "hf_alpha")
    speed       — speech rate multiplier (1.0 = normal)
    lang_code   — KPipeline language code for this row ('a' = American English)

    A per-call `language=` (ISO 639-1) picks another KPipeline, created lazily and
    cached per lang_code. Every pipeline shares the first one's KModel, so the
    weights load once; only the per-language G2P is new.

    defer_load  — build no KPipeline (and so load no weights) until the first synthesis, which
                  runs in the executor: for an instance made while the weights aren't on disk.
                  Missing files are fetched first in a subprocess that stop_download() can kill.
    """

    accepts_language = True

    def __init__(
        self,
        voice:     str = "af_sarah",
        speed:     float = 1.0,
        lang_code: str = "a",
        defer_load: bool = False,
    ) -> None:
        from kokoro import KPipeline
        self._KPipeline = KPipeline
        self._lang_code = lang_code
        self._pipelines: dict[str, Any] = {}
        if not defer_load:
            log.info("Loading Kokoro TTS voice=%s lang=%s", voice, lang_code)
            self._pipelines[lang_code] = KPipeline(lang_code=lang_code)
        self._voice    = voice
        self._speed    = speed
        self._lock     = asyncio.Lock()  # KPipeline is not thread-safe for concurrent calls
        self._last_fetch_failure: float | None = None
        self._warned_languages: set[str] = set()
        # lang_codes whose KPipeline failed to build (e.g. missing G2P extras): never retried.
        self._failed_lang_codes: set[str] = set()
        log.info("Kokoro TTS %s", "created, loads on first use" if defer_load else "ready")

    def _resolve_lang_code(self, language: Any) -> str:
        if language is INSTANCE_LANGUAGE or language is None:
            return self._lang_code
        # A Kokoro voice speaks only its own language: a request for another one (e.g. an
        # agent set to en-IN on a Hindi voice) keeps the row's pipeline, as before languages
        # reached Kokoro. Multilingual agents get a matching voice per language (validated).
        voice_language = _row_language(self._voice[:1].lower()) if self._voice else None
        if voice_language is not None and normalize_language(language) != voice_language:
            if language not in self._warned_languages:
                self._warned_languages.add(language)
                log.warning(
                    "Kokoro voice %s can't speak language=%r — using lang_code=%s",
                    self._voice, language, self._lang_code,
                )
            return self._lang_code
        if normalize_language(language) == _row_language(self._lang_code):
            return self._lang_code  # same language as the row: keep its own accent (e.g. British 'b')
        code = kokoro_lang_code(language)
        if code is None:
            if language not in self._warned_languages:
                self._warned_languages.add(language)
                log.warning("Kokoro has no pipeline for language=%r — using lang_code=%s", language, self._lang_code)
            return self._lang_code
        return code

    def _pipeline_sync(self, lang_code: str) -> Any:
        """Get or build the KPipeline for lang_code. Call under self._lock (in the executor).

        A build failure is logged once and falls back to the row's own pipeline: speaking
        with the wrong pronunciation beats silence on every sentence in that language."""
        pipeline = self._pipelines.get(lang_code)
        if pipeline is not None:
            return pipeline
        if self._lang_code not in self._pipelines:  # defer_load: the first synthesis loads the weights
            log.info("Kokoro: loading weights for voice=%s lang=%s", self._voice, self._lang_code)
            self._pipelines[self._lang_code] = self._KPipeline(lang_code=self._lang_code)
            if lang_code == self._lang_code:
                return self._pipelines[lang_code]
        base = self._pipelines[self._lang_code]
        if lang_code in self._failed_lang_codes:
            return base
        log.info("Kokoro: building pipeline lang_code=%s", lang_code)
        try:
            pipeline = self._KPipeline(lang_code=lang_code, model=base.model)
        except Exception:
            self._failed_lang_codes.add(lang_code)
            log.exception(
                "Kokoro: can't build pipeline lang_code=%s — using lang_code=%s instead",
                lang_code, self._lang_code,
            )
            return base
        self._pipelines[lang_code] = pipeline
        return pipeline

    async def _fetch_base_weights(self) -> None:
        """Before the base weights load: fetch them in a killable, shared subprocess if they aren't on
        disk (KPipeline would download ~330 MB on an executor thread nobody can stop). Voice files
        (~0.5 MB) are left to KPipeline's own lazy fetch, as before: they never wait on a shared
        download slot, so a synthesis is never queued behind a multi-GB model download."""
        if self.loaded:
            return
        if self._last_fetch_failure is not None and time.monotonic() - self._last_fetch_failure < _RETRY_AFTER_S:
            raise RuntimeError("Kokoro weights are not on disk (last download failed; retrying later)")
        files = ["config.json", "kokoro-v1_0.pth"]
        try:
            for filename in files:
                await asyncio.to_thread(hf_hub_download, KOKORO_REPO, filename, local_files_only=True)
        except LocalEntryNotFoundError:
            log.warning("Kokoro base weights are not on disk — downloading them now")
            try:
                await model_downloads.fetch(("kokoro", "base"), _DOWNLOAD_SCRIPT, KOKORO_REPO, *files)
            except Exception:
                self._last_fetch_failure = time.monotonic()
                log.exception("Kokoro base weights download failed")
                raise

    async def stop_download(self) -> None:
        """At shutdown: kill the downloads in flight (a synthesis waiting on one then fails)."""
        await model_downloads.stop_downloads()

    @property
    def loaded(self) -> bool:
        return self._lang_code in self._pipelines

    async def load(self) -> None:
        """Build the row's KPipeline (loading the weights) in the executor; a no-op once built."""
        async with self._lock:
            await self._fetch_base_weights()
            await asyncio.get_running_loop().run_in_executor(None, self._pipeline_sync, self._lang_code)

    async def prewarm(self, languages: list[str]) -> None:
        """Build the pipelines for these ISO languages now, so the first sentence
        in a new language doesn't pay the G2P load on the turn's critical path."""
        loop = asyncio.get_running_loop()
        for code in {self._resolve_lang_code(lang) for lang in languages}:
            if code in self._pipelines or code in self._failed_lang_codes:
                continue
            async with self._lock:
                await self._fetch_base_weights()
                await loop.run_in_executor(None, self._pipeline_sync, code)

    async def synthesize(self, text: str, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE) -> bytes:
        if not text.strip():
            return b""
        lang_code = self._resolve_lang_code(language)
        loop = asyncio.get_running_loop()
        async with self._lock:
            await self._fetch_base_weights()
            return await loop.run_in_executor(None, self._synthesize_sync, text, sample_rate, lang_code)

    async def synthesize_stream(self, text: str, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE):
        audio = await self.synthesize(text, sample_rate, language=language)
        if audio:
            yield audio

    def _synthesize_sync(self, text: str, sample_rate: int, lang_code: str | None = None) -> bytes:
        chunks: list[np.ndarray] = []

        pipeline = self._pipeline_sync(lang_code or self._lang_code)
        for _gs, _ps, audio in pipeline(
            text, voice=self._voice, speed=self._speed
        ):
            if audio is not None and len(audio) > 0:
                chunks.append(audio)

        if not chunks:
            return b""

        pcm_f32 = np.concatenate(chunks)

        # Resample from Kokoro's native 24 kHz to the requested sample_rate.
        if sample_rate != _KOKORO_NATIVE_RATE:
            pcm_f32 = self._resample(pcm_f32, _KOKORO_NATIVE_RATE, sample_rate)

        # Convert float32 [-1, 1] → int16 L16 PCM.
        pcm_i16 = np.clip(pcm_f32 * 32767, -32768, 32767).astype(np.int16)
        return pcm_i16.tobytes()

    @staticmethod
    def _resample(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
        from scipy.signal import resample_poly
        gcd = math.gcd(from_rate, to_rate)
        return resample_poly(audio, to_rate // gcd, from_rate // gcd).astype(np.float32)
