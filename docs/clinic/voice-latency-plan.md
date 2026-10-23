# Voice latency: research findings and plan

Status: proposal · Measured on the native server (acd-opensips-01), 2026-10-19 · Applies to the Medivista clinic agents and every agent on the platform.

**Bottom line:** a caller waits about **3.8 s** after they stop talking before the agent speaks, and about **5 s** when the agent calls a tool. Production voice agents run at roughly **0.8–1.2 s**. Two settings changes (Phase 1) should save about 1–1.4 s; streaming text-to-speech and dropping the second model call on tool turns (Phase 2) should bring a turn to about 1.5–1.8 s.

## 1. Where the time goes today

One typical turn from the 13:48 call on 2026-10-17, plus measurements taken on this server:

| Stage | Today | Cause |
|---|---|---|
| End-of-speech wait (Silero voice detection in the gateway) | **~1,000 ms** | `hold_windows=32` × 32 ms of silence. Not in any logged metric. |
| Speech-to-text final transcript (Deepgram nova-3, `multi`) | ~850 ms live; 370 ms in a clean test | Global endpoint `api.deepgram.com` routes to the US: **264 ms per round trip** from this server. Each finalisation needs several round trips. |
| AI model, time to first token (gpt-4o-mini) | 1.34 s median, 0.9–2.7 s | OpenAI serves from outside India. A tool turn makes **two** model calls. |
| Text-to-speech first audio (Sarvam Bulbul, REST) | 0.8–1.2 s | REST returns each sentence as one complete clip. |
| Clinic tools (toolexec + clinic service) | ~70 ms | Not a bottleneck. |
| **What the caller experiences** | **~3.8 s; ~5 s on tool turns** | |

### Measurements behind the table

Network round trip from this server (best of 3 TCP connects):

| Endpoint | Round trip |
|---|---|
| `api.deepgram.com` (global, US) | 264 ms |
| `api.in.deepgram.com` (India) | 50 ms |
| `api.sarvam.ai` | 35 ms |
| `api.openai.com`, `api.cartesia.ai`, `api.groq.com` | ~2 ms to the nearest edge; the model itself runs elsewhere |

Deepgram finalisation, same Hindi/Hinglish clip, nova-3 `multi`, 3 runs each:

| Endpoint | Finalise (ms) | Transcript |
|---|---|---|
| `api.deepgram.com` | 384, 393, 344 | identical |
| `api.in.deepgram.com` | 161, 159, 169 | identical |

Model time to first token with the real reception prompt (~2,350 tokens, tools attached), 4 runs each. OpenAI's prompt cache was already hitting about 2,200 tokens per call:

| Model | Median | Range |
|---|---|---|
| gpt-4o-mini (current) | 1,336 ms | 887–2,662 |
| gpt-4.1-mini | 842 ms | 756–919 |
| gpt-4.1-nano | 821 ms | 717–1,291 |
| gpt-4o | 784 ms | 733–1,023 |
| gpt-5-mini | 979 ms | 730–1,037 |
| gpt-5-nano | 764 ms | 750–916 |

About 0.75 s looks like the floor for OpenAI models served from outside India. Model quality on Hindi and Hinglish has not been compared yet.

Tool-call filler ("One moment.", "एक पल।"), before and after the Sarvam phrase cache (shipped in PR #131), from live call logs:

| Call | Tool call starts | Filler spoken | Sarvam request for the filler |
|---|---|---|---|
| 2026-10-17 13:49, before the cache | 13:49:13.158 | 13:49:13.626 (+468 ms) | yes, ~400 ms |
| 2026-10-19 06:30, with the cache | 06:30:44.663 | 06:30:44.664 (+1 ms) | none |

The filler no longer delays anything. It still plays on every tool call, though tools take ~70 ms, so callers hear about 1 s of "Give me a moment." before most answers (item 8). Today that second of audio overlaps the second model call, so it costs nothing extra; once item 6 removes that call, the ready answer would queue behind the filler. **Ship items 6 and 8 together.**

## 2. What others do

1. **Turn detection is the biggest lever.** Pipecat defaults to 0.2 s of silence; LiveKit uses 0.3–0.5 s plus a turn-detector model that judges whether the sentence is finished. Deepgram Flux Multilingual puts end-of-turn detection inside speech-to-text, supports Hindi, and claims under 400 ms. Hindi speakers pause longer, so a single English-tuned threshold cuts them off more often. We wait 1.0 s.
2. **Host everything in India.** LiveKit's India guide reports about 1 s saved end-to-end by running the agent, speech-to-text (Deepgram India) and the model (Azure OpenAI India or Gemini `asia-south1`) in Mumbai. Our Deepgram test above shows the same effect.
3. **Stream text-to-speech and start on short chunks.** Sarvam's WebSocket API plays the first chunk while the rest is generated (vendor claim: under 250 ms) over one connection per call. Start speaking on the first clause, not the whole sentence.
4. **Pick the model for speed and avoid a second model call.** Prompt caching (we already get it), a small model for simple turns, and tools that return ready-to-speak text so the second call is skipped.
5. **Prefetch and speculate.** Load predictable data at call start. More advanced: start likely tool calls from partial transcripts, or start generating before the turn is confirmed (LiveKit's "preemptive generation").
6. **Fillers only when needed.** Fillers hide slow tools, but used on every call they overlap the caller and slow tasks down.

## 3. Plan

### Phase 1: settings, about a day, low risk (estimate ~3.8 s → ~2.4 s)

| # | Change | Expected saving | Work |
|---|---|---|---|
| 1 | End-of-speech wait 1,000 ms → ~600 ms (tenant `vad_hold_ms`), then tune on real calls | ~400 ms | Setting + test calls |
| 2 | Deepgram live URL → `wss://api.in.deepgram.com/v1/listen` (make it a setting; `_LIVE_WS_URL` is hard-coded in `services/conversation/providers/stt/deepgram.py`) | ~200–600 ms | Small code change |
| 3 | Model gpt-4o-mini → gpt-4.1-mini or gpt-5-nano, after running the test-guide scenarios in Hindi and English | ~300–500 ms; removes 2.7 s spikes | Config + quality check |
| 4 | Add the greeting and other fixed sentences to Sarvam's prewarm | No ~1 s silence at pickup on the first call after a restart | Small |

### Phase 2: code, 3–5 days (estimate → ~1.5–1.8 s)

| # | Change | Expected saving |
|---|---|---|
| 5 | Sarvam WebSocket streaming text-to-speech, one connection per call, playback from the first clause | ~600–800 ms every turn |
| 6 | Availability and find answers spoken from the tool's ready-made sentence (success template), skipping the second model call, as booking and cancelling already do | ~0.8–1 s on tool turns |
| 7 | At call start, look up the caller's upcoming appointments and give them to the model, so "reschedule my appointment" needs no lookup call | One tool round trip on reschedule/cancel calls |
| 8 | Skip the filler when a tool usually takes under ~400 ms (the latency store already records this); keep it for slow tools. The cache already removed its synthesis delay. | ~1 s less audio before most tool answers; fewer overlaps with the caller |
| 9 | Per-stage timing in logs, including the end-of-speech wait and the moment the caller hears audio, at p50 and p95 | Lets every later change be measured |

### Phase 3: larger, 1–2 weeks each, after Phase 2 is measured (target ~0.9–1.2 s)

| # | Change | Why |
|---|---|---|
| 10 | Semantic turn detection: Deepgram Flux Multilingual (Hindi, built-in end-of-turn) or a Smart Turn model in the gateway, with the silence wait at ~300 ms | End-of-speech wait from ~600 ms to ~200–400 ms without cutting callers off |
| 11 | Model hosted in India: Azure OpenAI India or Gemini on Vertex `asia-south1` (needs a cloud account) | India-hosted models report 180–300 ms to first token, against our ~750 ms floor |
| 12 | Preemptive generation: start the model on Deepgram's final transcript before the end-of-speech wait ends, and discard it if the caller keeps talking | Hides most of the remaining wait |

### What about caching the clinic API calls?

The clinic APIs take about 70 ms in total, so caching their results would save little and risks offering a slot someone has just booked. The worthwhile API-side changes are #6 (no second model call after a tool) and #7 (prefetching the caller's bookings).

## 4. Recommendation

1. Do Phase 1 now, starting with #1 and #2: together about 0.6–1.0 s for almost no risk.
2. Then #5 (Sarvam streaming) and #6 (no second model call), which cover most of the remaining delay.
3. Phase 3 needs decisions: a Deepgram Flux trial on recorded Hindi calls, and an Azure India or GCP Mumbai account for an India-hosted model.

Make one change at a time and measure each with a test call before the next.

## 5. Notes and caveats

- Several latency figures in the sources are vendor-reported; ours in section 1 are measured on this server.
- A Deepgram "keep one socket open and send Finalize" change was tried on 2026-10-17 and reverted: it once dropped half an utterance, and the gain was small next to switching to the India endpoint.
- Deepgram's India routing covers only some models and languages (nova-3 general, including Hindi and `multi`, per the docs quoted in the sources). Check this before relying on it for other models such as Flux.

## Sources

- [Hamming: Voice AI latency, what's fast and slow](https://hamming.ai/resources/voice-ai-latency-whats-fast-whats-slow-how-to-fix-it)
- [ElevenLabs: Voice agent latency optimization](https://elevenlabs.io/blog/voice-agent-latency-optimization)
- [FutureAGI: 12 techniques for voice agent latency](https://futureagi.com/blog/how-to-optimize-voice-agent-latency-2026/)
- [Caller.digital: Sub-500 ms voice AI in India](https://caller.digital/blog/sub-500ms-latency-voice-ai-india-architecture-stt-llm-tts-2026)
- [LiveKit: Building performant voice agents in India](https://livekit.com/blog/building-performant-voice-agents-india)
- [LiveKit: Turn-taking tuning](https://docs.livekit.io/agents/logic/turns/tuning.md)
- [LiveKit: Transformer end-of-turn detection](https://livekit.com/blog/using-a-transformer-to-improve-end-of-turn-detection)
- [LiveKit: VAD and turn detection configuration](https://livekit.com/field-guides/guide/vad-turn-detection-configuration)
- [Pipecat: Speech input](https://docs.pipecat.ai/pipecat/learn/speech-input)
- [Smart Turn v3.2](https://huggingface.co/soniqo/Smart-Turn-v3.2-ONNX)
- [Deepgram: Flux Multilingual](https://deepgram.com/learn/introducing-flux-multilingual)
- [Sarvam: Text-to-speech WebSocket streaming](https://docs.sarvam.ai/api-reference-docs/api-guides-tutorials/text-to-speech/streaming-api/web-socket)
- [Sarvam: Streaming speech-to-text](https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/streaming-api)
- [Bolna: Indian server configuration](https://www.bolna.ai/docs/indian-server-configuration)
- [Stream: Speculative tool calling](https://getstream.io/blog/speculative-tool-calling-voice/)
- [arXiv: Hiding tool latency through speculative execution](https://arxiv.org/html/2610.07641)
- [DEV: 5 tricks to hide voice agent latency](https://dev.to/kenimo49/your-voice-agent-is-slow-here-are-5-tricks-to-hide-it-3pcb)
- [Superteams: Function calling in voice agents](https://www.superteams.ai/courses/building-voice-ai-agents-in-production/08-function-calling-in-voice-agents/)
- [Roark: Testing filler phrases](https://roark.ai/blog/testing-filler-phrases-voice-agents)
- [Full-Duplex-Bench-v3](https://arxiv.org/pdf/2604.04847)
