"""
VAD SDK — voice activity detection, extracted from services/vobiz/ so a
future telephony bridge (Twilio, Telnyx, ...) can reuse the same detection
logic instead of duplicating it or importing from another provider's
package (found via analyzing Dograh/pipecat's architecture, 2026-08-02:
their VADAnalyzer is transport-agnostic — operates only on raw PCM frames,
with each transport's own serializer responsible for getting audio into
that common format first).

Two independent detectors, not a pluggable-provider registry like
telephony_sdk/config_sdk/knowledge_sdk — there's no per-vendor swapping
here, just a real model (SileroVAD) and a fallback (EnergyVAD) a caller
picks directly. See each module's own docstring for why both exist.

What deliberately stays OUT of this package: barge-in *reaction* (clearing
a playback queue, sending CancelGeneration, pre-roll buffering) — that's
transport-specific pipeline behavior, not detection, and belongs in each
bridge (see services/vobiz/bridge.py) the same way pipecat's own
InterruptionFrame reaction is transport-specific even though the frame
itself is generic.
"""
