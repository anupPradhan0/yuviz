"""Measure one conversation turn end to end: the real handler with real Deepgram and OpenAI and a simulated voice.

The voice is simulated (fixed time to first audio, VOICE_MS) so the numbers show the pipeline's own behaviour and
do not depend on a voice provider's account or load. Run from the repository root of the code under test, once per
branch, and compare:

    DG_KEY=... OAI_KEY=... AUDIO=/path/to/wavs python scripts/bench_voice_turn.py "<label>"

AUDIO holds q1.wav and q2.wav: 16 kHz mono 16-bit questions (on macOS: `say "..." -o q1.aiff` then
`afconvert -f WAVE -d LEI16@16000 -c 1 q1.aiff q1.wav`). Optional: MODEL (default gpt-4.1-mini), REPS (3),
HANG_MS (700, the caller-silence wait before "speech ended"), VOICE_MS (300).
Times are from the caller's last word. This calls paid APIs a handful of times per run.
"""
import asyncio, os, sys, time, wave, statistics, logging, json
from services.conversation.tests.test_pipeline import _make_handler
from services.conversation.providers.stt.deepgram import DeepgramSTT
from services.conversation.providers.llm.openai import OpenAILLM

AUDIO = os.environ["AUDIO"]; HANG_MS = int(os.environ.get("HANG_MS", "700")); VOICE_MS = int(os.environ.get("VOICE_MS", "300"))
SIL = b"\x00\x00" * 320
FILLERS = ("Mm-hmm, one moment.",)

def pcm(name):
    with wave.open(f"{AUDIO}/{name}.wav") as w: return w.readframes(w.getnframes())

class SimTTS:
    """Cloud-voice stand-in: fixed time to first audio, one chunk per sentence."""
    def __init__(self): self.log = []
    async def synthesize_stream(self, text, sample_rate):
        self.log.append(text)
        await asyncio.sleep(VOICE_MS / 1000)
        yield (b"F" if text in FILLERS else b"A") * 3200

async def feed(handler, sid, data, buf):
    for i in range(0, len(data), 640):
        chunk = data[i:i+640]; buf.extend(chunk)
        await handler.on_audio(sid, chunk); await asyncio.sleep(0.02)

async def one_call(label, stt, llm, tts, questions):
    handler = _make_handler(stt, llm, tts, greeting="Hello, how can I help you today?",
                            system_prompt="You are a concise voice assistant. Answer in one short sentence.")
    sid = f"{label}-{time.time_ns()}"
    t = time.perf_counter(); await handler.greeting(sid); greet_ms = (time.perf_counter() - t) * 1000
    await asyncio.sleep(1.2)                                        # the caller listens, then speaks
    rows = []
    for q in questions:
        buf = bytearray()
        await feed(handler, sid, pcm(q), buf)
        t_end = time.perf_counter()                                 # the caller's last word
        for _ in range(HANG_MS // 20):                              # silence is streamed during the hangover
            buf.extend(SIL); await handler.on_audio(sid, SIL); await asyncio.sleep(0.02)
        t_call = time.perf_counter()                                # the client says "speech ended"
        t_any = t_ans = None; text = ""
        async for r in handler.on_speech_ended(sid, bytes(buf), 1000, -20.0):
            now = time.perf_counter()
            if r.stt_text: text = r.stt_text
            if r.tts_payloads:
                kind = r.tts_payloads[0][:1]
                if t_any is None: t_any = now
                if kind == b"A" and t_ans is None: t_ans = now
        rows.append(dict(q=q, text=text[:40], any=(t_any - t_end) * 1000 if t_any else None,
                         answer=(t_ans - t_end) * 1000 if t_ans else None,
                         since_speech_ended=(t_ans - t_call) * 1000 if t_ans else None))
        await asyncio.sleep(1.0)
    await handler.on_session_end(sid, "bench")
    return greet_ms, rows

async def main(label):
    stt = DeepgramSTT(api_key=os.environ["DG_KEY"], model="nova-3", language="en")
    llm = OpenAILLM(api_key=os.environ["OAI_KEY"], model=os.environ.get("MODEL", "gpt-4.1-mini"),
                    system="You are a concise voice assistant.", temperature=0.2)
    tts = SimTTS()
    questions = ["q1", "q2", "q1"]
    all_rows = []; greets = []
    for rep in range(int(os.environ.get("REPS", "3"))):
        g, rows = await one_call(label, stt, llm, tts, questions)
        greets.append(g); all_rows.append(rows)
    print(f"== {label}")
    print("greeting ms per call:", [round(g) for g in greets])
    for turn in range(3):
        a = [r[turn]["answer"] for r in all_rows if r[turn]["answer"]]
        n = [r[turn]["any"] for r in all_rows if r[turn]["any"]]
        s = [r[turn]["since_speech_ended"] for r in all_rows if r[turn]["since_speech_ended"]]
        print(f"turn {turn+1}: first audio {statistics.median(n):6.0f} ms | first ANSWER audio {statistics.median(a):6.0f} ms "
              f"(from last word) | {statistics.median(s):6.0f} ms after speech_ended | runs {[round(x) for x in a]}")
asyncio.run(main(sys.argv[1]))
