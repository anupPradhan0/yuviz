"""
SileroVAD — faithful Python port of the Gateway's own SileroVAD
(gateway/include/media/SileroVAD.h, gateway/src/media/SileroVAD.cpp), not a
fresh reimplementation.

Moved here from services/vobiz/ (2026-08-02) so a future telephony bridge
can reuse this detector instead of duplicating it or reaching into
Vobiz's own package — this class has no Vobiz-specific knowledge at all,
it only ever consumed raw PCM16 and returned VADEvent. See libs/vad_sdk's
own __init__.py for the architectural reasoning (informed by analyzing
Dograh/pipecat's transport-agnostic VADAnalyzer).

Why this exists at all: EnergyVAD (vad.py) is a pure amplitude threshold —
it cannot distinguish loud noise/echo from real speech. Confirmed live on a
real Vobiz call: energy readings of -13dB to -32dB (well above EnergyVAD's
-35dB threshold) with no caller actually speaking, one of which got
transcribed by Whisper as "Bye bye children" — a well-known Whisper
hallucination pattern specifically seen on non-speech/silent audio,
confirming the underlying signal genuinely wasn't speech. This is exactly
the failure mode the Gateway itself already solved: EnergyVAD is kept
around only as a fallback; SileroVAD (a real neural model that classifies
speech vs. non-speech, not just loudness) is the default for real
telephony. Porting the same model here closes that gap for any bridge
that streams continuous audio with no client-side speech_ended signal
(unlike services/webcall/, which relies on the browser for that).

Model: models/silero_vad.onnx — Silero v6, 16kHz-only export (no sample-
rate input tensor; confirmed identical to the copy faster-whisper itself
bundles at venv/.../faster_whisper/assets/silero_vad_v6.onnx). Windowing:
fixed 512-sample (32ms) windows, each concatenated with 64 samples of
trailing context from the previous window (576-float model input total).
LSTM state (h/c, [1,1,128] each) is carried across windows for the life
of the call, exactly like the C++ implementation's context_/h_/c_.

process() must be called with exactly one window's worth of audio (512
samples / 1024 bytes of 16-bit PCM at 16kHz / 32ms) — same "one frame per
call" contract EnergyVAD already uses, just a different frame size, so
bridge.py's existing buffering loop only needs its frame-size constant
changed, not its structure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort

from .vad import VADEvent

log = logging.getLogger(__name__)

_MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "silero_vad.onnx"
WINDOW_SAMPLES = 512   # 32ms @ 16kHz — Silero's fixed window size
WINDOW_BYTES = WINDOW_SAMPLES * 2  # 16-bit PCM
_CONTEXT_SAMPLES = 64
_WINDOW_MS = 32
_STATE_SHAPE = (1, 1, 128)


@dataclass(frozen=True)
class SileroVADConfig:
    speech_threshold: float = 0.5
    silence_threshold: float = 0.35
    onset_ms: int = 96
    # 1000, not the commonly-misremembered 500 — gateway/include/media/
    # SileroVADConfig.h's own comment: both 500ms and 700ms split
    # utterances at natural mid-sentence pauses in live testing.
    hold_ms: int = 1000


class SileroVAD:
    def __init__(self, cfg: SileroVADConfig | None = None) -> None:
        self._cfg = cfg or SileroVADConfig()
        self._onset_needed = max(1, -(-self._cfg.onset_ms // _WINDOW_MS))  # ceil div
        self._hold_needed = max(1, -(-self._cfg.hold_ms // _WINDOW_MS))

        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = 1
        sess_options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(_MODEL_PATH), sess_options=sess_options, providers=["CPUExecutionProvider"],
        )

        self._context = np.zeros(_CONTEXT_SAMPLES, dtype=np.float32)
        self._h = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._c = np.zeros(_STATE_SHAPE, dtype=np.float32)

        self._in_speech = False
        self._onset_windows = 0
        self._silence_windows = 0
        self._speech_windows = 0
        self._fail_count = 0
        self.last_speech_prob = 0.0

    def process(self, pcm16_window: bytes) -> VADEvent:
        if len(pcm16_window) != WINDOW_BYTES:
            log.warning("SileroVAD: expected exactly %d bytes, got %d — dropping window", WINDOW_BYTES, len(pcm16_window))
            return VADEvent.NONE

        samples = np.frombuffer(pcm16_window, dtype=np.int16).astype(np.float32) / 32768.0
        model_input = np.concatenate([self._context, samples]).reshape(1, -1).astype(np.float32)
        self._context = samples[-_CONTEXT_SAMPLES:]

        try:
            prob, self._h, self._c = self._session.run(
                ["speech_probs", "hn", "cn"],
                {"input": model_input, "h": self._h, "c": self._c},
            )
        except Exception:
            self._fail_count += 1
            if self._fail_count % 100 == 1:
                log.exception("SileroVAD: inference failed (count=%d)", self._fail_count)
            return VADEvent.NONE

        self.last_speech_prob = float(np.asarray(prob).reshape(-1)[0])

        if not self._in_speech:
            if self.last_speech_prob >= self._cfg.speech_threshold:
                self._onset_windows += 1
                if self._onset_windows >= self._onset_needed:
                    self._in_speech = True
                    self._silence_windows = 0
                    self._speech_windows = self._onset_windows
                    self._onset_windows = 0
                    return VADEvent.SPEECH_START
            else:
                self._onset_windows = 0
            return VADEvent.NONE

        if self.last_speech_prob < self._cfg.silence_threshold:
            self._silence_windows += 1
            if self._silence_windows >= self._hold_needed:
                self._in_speech = False
                self._silence_windows = 0
                self._speech_windows = 0
                return VADEvent.SPEECH_END
        else:
            self._silence_windows = 0
            self._speech_windows += 1
        return VADEvent.NONE

    @property
    def speech_duration_ms(self) -> int:
        return self._speech_windows * _WINDOW_MS if self._in_speech else 0

    def reset(self) -> None:
        self._context = np.zeros(_CONTEXT_SAMPLES, dtype=np.float32)
        self._h = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._c = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._in_speech = False
        self._onset_windows = 0
        self._silence_windows = 0
        self._speech_windows = 0
        self.last_speech_prob = 0.0
