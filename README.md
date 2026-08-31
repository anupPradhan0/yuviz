# Voice AI Platform

A voice AI platform: real-time STT → LLM (with tool-calling: calendar
booking, RAG, AI-to-human transfer) → TTS, reachable either over real SIP
telephony (via the C++ Gateway described below) or, for local development
and testing without any telephony infra, directly from a browser.

## Quickstart

Docker is the only prerequisite — and if you don't have it, the script offers to
install Docker or Colima for you.

```bash
git clone https://github.com/yuviz-ai/yuviz.git && cd yuviz
./deployment/sh/dev.sh
```

On Windows use `.\deployment\sh\dev.ps1` (PowerShell) or
`deployment\sh\dev.cmd` (Command Prompt) — both re-invoke the same script
through Git Bash or WSL.

Then open <http://localhost:3000>. The first run asks you to
**create your administrator account** — there are no default credentials
shipped with this repo. After that, pick the `default` agent and click
**Test Agent** to talk to it in your browser.

`./deployment/sh/dev.sh` checks your environment, generates `deployment/.env` with random secrets,
builds the images, pulls the models, waits for every service to report
healthy, and then verifies STT, LLM and TTS actually work end to end before
telling you it's ready. First run downloads ~3 GB of models and needs **16 GB
of free disk** (~15.6 GB of images and models); later runs skip anything
already cached.

```bash
./deployment/sh/dev.sh --logs        # tail logs
./deployment/sh/dev.sh --down        # stop, keep data
./deployment/sh/dev.sh --clean       # stop and wipe volumes
./deployment/sh/dev.sh --verbose     # full build/pull output
./deployment/sh/dev.sh --timeout 600 # slower machines / CI
./deployment/sh/dev.sh --version     # versions, for bug reports
```

### Running only the parts you need

The three local models are what make this stack heavy — llama3.2 on CPU will
peg a laptop on its own. Turn off any you don't need; everything else runs
exactly as normal.

```bash
./deployment/sh/dev.sh --no-llm      # no Ollama container, no llama3.2 (~2 GB)
./deployment/sh/dev.sh --no-stt      # no speech-to-text, no Whisper (~500 MB)
./deployment/sh/dev.sh --no-tts      # no text-to-speech, no Kokoro (~313 MB)
```

Combine them freely — `--no-llm --no-stt --no-tts` starts the platform with no
local inference at all. A disabled model is not downloaded, not loaded at
startup, and dropped from the verification step; `--no-llm` also stops an
Ollama container left over from an earlier run. Model caches are kept either
way, so dropping a flag later costs nothing but the restart.

With `--no-llm`, add a cloud provider key under **AI & Voice** in the Admin UI
and point your agent at it. That is the usual setup once you're past the first
look around: the LLM does the thinking, so it's the one worth moving off your
own CPU.

**Using a Mac with Apple Silicon?** Containers can't reach Metal, so the
containerized Ollama runs the LLM on CPU. For GPU speed, run Ollama on the
host and point the stack at it:

```bash
ollama serve                      # in another terminal
USE_HOST_OLLAMA=1 ./deployment/sh/dev.sh
```

Full details — container list, resource requirements, configuration reference
and troubleshooting — are in
[docs/docker-startup.md](docs/docker-startup.md).

Prefer a native install without Docker? [docs/setup.md](docs/setup.md) covers
that path. Note it was only ever verified on macOS.

Both paths cover the **web-testing** subset — the Python services plus
admin-ui's browser call panel. The rest of this file documents the C++
Gateway, which handles real phone calls (Kamailio/FreeSWITCH) and is not part
of the fork-and-run path.

## Voice AI Gateway (C++)

Carrier-grade gateway between FreeSWITCH and AI services (STT / LLM / TTS).

## Architecture

```
Linphone/Zoiper → Kamailio → FreeSWITCH → mod_audio_fork
                                                  │
                                         WebSocket (PCM)
                                                  │
                                         C++ Gateway (this)
                                                  │
                                         gRPC (via Envoy)
                                                  │
                                         Python AI Services
```

## Modules

| Module       | Purpose                                              |
|--------------|------------------------------------------------------|
| application  | Lifecycle, signal handling, dependency wiring        |
| config       | YAML configuration (yaml-cpp)                        |
| logging      | Structured logging wrapper (spdlog)                  |
| websocket    | libwebsockets server — accepts FreeSWITCH connections|
| session      | Per-call state machine; owns MediaPipeline           |
| media        | Lock-free ring buffer; drain thread per session      |
| dispatcher   | Routes AudioFrames to transport; decouples media     |
| telephony    | ESL client/listener — hangup, AI-to-human transfer   |
| transport    | IConversationTransport; gRPC + Null (echo) impls     |
| metrics      | IMetrics interface; in-process counters/gauges       |
| utils        | ThreadPool, SPSC RingBuffer<T>                       |

## Build

### macOS (Apple Silicon)

```bash
brew install spdlog yaml-cpp nlohmann-json libwebsockets googletest cmake

cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(sysctl -n hw.logicalcpu)
```

### Linux

```bash
apt install -y libspdlog-dev libyaml-cpp-dev nlohmann-json3-dev \
               libwebsockets-dev libgtest-dev cmake

cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
```

## Run

```bash
./build/gateway/voice_ai_gateway config/gateway.yaml
```

## Test

```bash
ctest --test-dir build --output-on-failure
```

## Design Principles

- C++20, RAII everywhere, `unique_ptr` by default
- No global mutable state (exception: signal handler atomic flag)
- All interfaces are pure virtual — implementations are swappable
- Media threads never block; SPSC ring buffer between receive and drain
- Dispatcher decouples WebSocket receive from transport I/O
