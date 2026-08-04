"""
Provider interfaces for the STT → LLM → TTS pipeline.

All providers are async and designed for streaming where possible:
  ISTT  — batch transcription (receives accumulated PCM, returns transcript)
  ILLM  — streaming text generation (yields tokens)
  ITTS  — batch synthesis per sentence (receives text, returns PCM bytes)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Protocol


@dataclass
class SttResult:
    text:       str
    confidence: float = 1.0


@dataclass
class ChatMessage:
    role:    str   # "system" | "user" | "assistant" | "tool"
    content: str
    # Tool-calling extension (additive, optional — every existing call site
    # is unaffected). Set on an assistant-role message that made one or
    # more tool calls this turn (ToolCallOrchestrator constructs this when
    # folding a ToolCallEvent back into history); each entry is
    # {"id": tool_call_id, "name": tool_name, "arguments": dict}.
    tool_calls: list[dict[str, Any]] | None = field(default=None)
    # Set on a "tool"-role message — which tool_calls entry this result
    # answers. Every ILLM implementation's plain generate() ignores both
    # fields entirely (they only ever read .role/.content); only
    # generate_with_tools() implementations look at them, each translating
    # into its own vendor's native tool-result wire shape (see
    # ollama.py/gemini.py — Ollama has a real "tool" role, Gemini submits a
    # functionResponse part on a "user"-role turn instead; this is exactly
    # the kind of vendor difference each provider already bridges itself,
    # not something ToolCallOrchestrator should know about).
    tool_call_id: str | None = field(default=None)


class ISTT(Protocol):
    """Transcribe accumulated PCM audio to text."""

    async def transcribe(self, audio: bytes, sample_rate: int) -> SttResult:
        """
        audio       — raw L16 PCM bytes
        sample_rate — samples per second (e.g. 16000)
        Returns SttResult with text="" when nothing was recognised.
        """
        ...

    async def feed_stream(self, session_id: str, chunk: bytes, sample_rate: int) -> None:
        """Forward one audio chunk the instant it arrives, before the
        utterance boundary (speech_ended) is even known — see pipeline.py's
        on_audio(), called on every inbound AudioChunk. Real, measured
        2026-08-02: batch-only STT (Deepgram's own pre-recorded /v1/listen,
        called only after speech_ended, same as local Whisper) throws away
        Deepgram's actual advantage — transcribing continuously while the
        caller is still talking — so finalize_stream() ends up doing a full
        decode from scratch instead of just picking up whatever's already
        in flight. A provider with no genuine live-streaming API
        (FasterWhisperSTT today) has nothing useful to do per chunk — the
        whole buffer arrives via finalize_stream's `audio` param anyway,
        same as before this method existed — so it's a no-op there."""
        ...

    async def finalize_stream(self, session_id: str, audio: bytes, sample_rate: int) -> SttResult:
        """Called once speech_ended fires. `audio`/`sample_rate` are the
        same full accumulated buffer transcribe() always received — a
        provider with a genuine live stream (Deepgram) already has
        everything it needs from feed_stream() and ignores them; a
        provider without one (FasterWhisperSTT) uses them for the same one
        batch transcribe() call as before."""
        ...

    async def cancel_stream(self, session_id: str) -> None:
        """The session ended or was cancelled without a clean
        finalize_stream (e.g. the call dropped mid-utterance) — release any
        per-session streaming state (a live WebSocket, in Deepgram's case)
        instead of leaking it. No-op for a provider with no per-session
        state to release."""
        ...


class ILLM(Protocol):
    """Generate a streaming text response from a conversation history."""

    def generate(
        self, messages: list[ChatMessage]
    ) -> AsyncGenerator[str, None]:
        """Yield text tokens as they are produced."""
        ...


class ITTS(Protocol):
    """Synthesise a text string to raw L16 PCM bytes at a given sample rate."""

    async def synthesize(self, text: str, sample_rate: int) -> bytes:
        """Returns raw L16 PCM at sample_rate Hz, or empty bytes on error."""
        ...

    def synthesize_stream(self, text: str, sample_rate: int) -> AsyncGenerator[bytes, None]:
        """Yield raw L16 PCM chunks at sample_rate Hz as they become
        available, instead of waiting for the complete utterance (see
        pipeline.py's _llm_to_tts, which forwards each yielded chunk to the
        caller immediately — real, measured 2026-08-01: Deepgram's own
        /v1/speak response streams progressively server-side (first byte at
        ~800ms, last byte at ~1600ms for one sentence), but our old
        synthesize()-only path threw that away by blocking on the full
        response body before returning anything. A provider with no genuine
        incremental synthesis (macOS/Kokoro/ElevenLabs today) just yields
        its one complete synthesize() result once — still correct, just not
        faster; only Deepgram's implementation does real chunk-by-chunk
        streaming."""
        ...
