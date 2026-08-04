"""
Energy-based VAD — a faithful Python port of the Gateway's own
EnergyVAD/EnergyVADConfig (gateway/include/media/EnergyVAD.{h,cpp}), not a
fresh invention.

Moved here from services/vobiz/ (2026-08-02) — this class has no
Vobiz-specific knowledge at all, it only ever consumed raw PCM16 and
returned VADEvent, so it belongs in a shared package any future telephony
bridge can import directly. See libs/vad_sdk's own __init__.py for why.

webcall's push-to-talk model (the browser UI decides when an utterance
ends and sends a "speech_ended" control message) doesn't apply to a real
telephony bridge: a provider like Vobiz streams continuous audio with no
such signal, so the bridge needs the same real-time speech-start/
speech-end detection real telephony already gets from the C++ Gateway,
just running in this Python process instead.

Same defaults as EnergyVADConfig: speech starts once energy exceeds
-35dB for a sustained 100ms (onset_ms) — a single loud frame is
indistinguishable from an echo blip — and ends once energy stays below
-40dB for 500ms (hold_ms). Frame size is 20ms, matching the Gateway's own
frame_ms and this bridge's own send cadence.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from enum import Enum, auto


class VADEvent(Enum):
    NONE = auto()
    SPEECH_START = auto()
    SPEECH_END = auto()


@dataclass(frozen=True)
class EnergyVADConfig:
    speech_threshold_db: float = -35.0
    silence_threshold_db: float = -40.0
    hold_ms: int = 500
    frame_ms: int = 20
    onset_ms: int = 100


class EnergyVAD:
    def __init__(self, cfg: EnergyVADConfig | None = None) -> None:
        self._cfg = cfg or EnergyVADConfig()
        self._hold_frames = max(1, self._cfg.hold_ms // self._cfg.frame_ms)
        self._onset_needed = max(1, -(-self._cfg.onset_ms // self._cfg.frame_ms))  # ceil div

        self._in_speech = False
        self._silence_frames = 0
        self._speech_frames = 0
        self._onset_frames = 0
        self.last_energy_db = -96.0

    def process(self, pcm16_frame: bytes) -> VADEvent:
        """pcm16_frame must be one frame_ms-worth of 16-bit signed PCM,
        mono (320 samples / 640 bytes at 16kHz for the default 20ms)."""
        sample_count = len(pcm16_frame) // 2
        if sample_count == 0:
            return VADEvent.NONE

        self.last_energy_db = self._compute_energy_db(pcm16_frame, sample_count)

        if not self._in_speech:
            if self.last_energy_db >= self._cfg.speech_threshold_db:
                self._onset_frames += 1
                if self._onset_frames >= self._onset_needed:
                    self._in_speech = True
                    self._silence_frames = 0
                    self._speech_frames = self._onset_frames
                    self._onset_frames = 0
                    return VADEvent.SPEECH_START
            else:
                self._onset_frames = 0
            return VADEvent.NONE

        # Currently in speech.
        if self.last_energy_db < self._cfg.silence_threshold_db:
            self._silence_frames += 1
            if self._silence_frames >= self._hold_frames:
                self._in_speech = False
                self._silence_frames = 0
                self._speech_frames = 0
                return VADEvent.SPEECH_END
        else:
            self._silence_frames = 0
            self._speech_frames += 1
        return VADEvent.NONE

    @property
    def speech_duration_ms(self) -> int:
        return self._speech_frames * self._cfg.frame_ms if self._in_speech else 0

    def reset(self) -> None:
        self._in_speech = False
        self._silence_frames = 0
        self._speech_frames = 0
        self._onset_frames = 0
        self.last_energy_db = -96.0

    @staticmethod
    def _compute_energy_db(pcm16_frame: bytes, sample_count: int) -> float:
        samples = struct.unpack(f"<{sample_count}h", pcm16_frame[: sample_count * 2])
        total = sum((s / 32768.0) ** 2 for s in samples)
        rms = math.sqrt(total / sample_count)
        if rms < 1e-10:
            return -96.0
        return 20.0 * math.log10(rms)
