# Voice AI Platform

A voice AI platform: real-time STT → LLM (with tool-calling: calendar
booking, RAG, AI-to-human transfer) → TTS, reachable either over real SIP
telephony (via the C++ Gateway described below) or, for local development
and testing without any telephony infra, directly from a browser.

**New to this repo?** See [docs/setup.md](docs/setup.md) — it covers the
web-testing path (Postgres/Redis + the Python services + admin-ui's "Test
Agent" browser panel), which is what a fresh fork can actually run without
any native SIP/telephony install. The rest of this file documents the C++
Gateway specifically, which is the piece that handles real phone calls
(Kamailio/FreeSWITCH) and is not part of that fork-and-run path.

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
