# Voice latency: what the conversation service does to stay fast

This covers the speech-to-text connection, the greeting and filler audio, and the filler timing. For where time goes in a
turn, read `services/conversation/pipeline.py` (`on_speech_ended`).

## What happens at call start

While the greeting is being synthesized, the service opens the Deepgram live connection (`prewarm_stream`) and starts the
knowledge prewarm. After the greeting it prepares the filler phrases once per voice. The first caller sentence therefore
does not pay Deepgram's connect time (about 0.8 s measured) or a voice round trip for a filler.

## Speech-to-text (Deepgram)

* One live connection per call, kept open across utterances (KeepAlive during silence). It used to be closed and reopened
  for every utterance.
* End of utterance: when Deepgram has already reported the caller's endpoint (`speech_final`) and no new words have arrived
  since, the transcript is complete and no flush is sent. Otherwise a `Finalize` message flushes it (about 0.3 s). The old
  code always closed the stream and waited for the server to close it.
* If the connection is gone, the buffered utterance is transcribed over REST instead of being lost.
* Optional `endpointing_ms` (10 to 5000) in the STT provider's `extra`. Unset keeps Deepgram's default, which reported the
  endpoint about 400 ms after the end of speech in testing; a value of 300 was slower, not faster, so leave it unset unless
  you measure a benefit.

## Fixed phrases are cached as audio

The greeting and the filler phrases are synthesized once per TTS instance (voice, model and speed) and replayed. A repeat
call's greeting has no synthesis delay. The cache lives with the TTS instance, so a provider config change starts it empty.
It holds at most `VOICEAI_PHRASE_CACHE_ENTRIES` phrases per instance (default 64).

## Fillers

* **First turn:** the model call starts at once. A short filler plays only if the model has produced nothing after
  `VOICEAI_FILLER_DELAY_MS` (default 400). A model that answers sooner gets no filler. The filler comes from cached audio,
  so it starts immediately when the delay ends. The filler is never counted as the first audio of the answer in the
  stored latencies.
* **Tools:** a filler is requested only if the tool is still running after `VOICEAI_TOOL_FILLER_DELAY_MS` (default 400);
  this applies to remote tools too, which used to ask for one at dispatch. A tool that finishes sooner is still counted as
  a call (the booking guard needs that) but stays silent.

## Measuring

`scripts/bench_voice_turn.py` runs the real handler with real Deepgram and OpenAI and a simulated 300 ms voice. Run it on
two branches and compare. Results from this change (median of 3 calls, `gpt-4.1-mini`, last word to first answer audio):

| | before | after |
|---|---|---|
| Greeting on a repeat call | 301 ms | 0 ms |
| Turn 1 | 2693 ms | 1801 ms |
| Turn 2 | 2323 ms | 1912 ms |
| Turn 3 | 2183 ms | 1895 ms |
| Slowest of 9 runs | 12.7 s | 1.96 s |

These include the 700 ms the client waits for silence. They were measured on a development laptop; absolute numbers
will differ on a server.
