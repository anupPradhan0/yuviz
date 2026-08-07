# Docker Startup Guide

Everything needed to run this project as a local development stack. Docker is
the only prerequisite — no Python, Node, Postgres, Redis or Ollama install
required on the host.

Works on **Linux, macOS and Windows**. Verified end-to-end on Linux
(Arch, x86_64, Docker 29.6, Compose 5.3).

---

## 1. Quick start

```bash
git clone https://github.com/yuviz-ai/yuviz.git
cd yuviz
./deployment/sh/dev.sh
```

Then open <http://localhost:3000> and log in:

| Field | Value |
|---|---|
| Email | `admin@yuviz.local` |
| Password | `admin123` |

Select the `default` agent → click **Test Agent** → allow microphone → talk.

First run takes **10–30 minutes** (image build + ~3 GB of models).
Every run after that takes **~75 seconds**.

---

## 2. Requirements

### Hardware

| | Minimum | Recommended |
|---|---|---|
| RAM | 8 GB | 16 GB |
| Free disk | 16 GB | 25 GB+ |
| CPU | 4 cores | 8+ cores |

The stack idles at **~4.9 GB RAM**. `dev.sh` refuses to start below **16 GB
free disk** rather than failing halfway through a download — the images and
models come to ~15.6 GB (see §4).

No GPU is needed. In the default containerized mode everything runs on CPU,
including a CPU-only build of PyTorch, and a host GPU is not used even when
present. The one exception is `USE_HOST_OLLAMA=1` (see §8), which runs the LLM
on the host and can use its GPU; STT and TTS stay on CPU either way.

### Software

| | Minimum version | Why |
|---|---|---|
| Docker Engine | 20.10 | — |
| Docker Compose | **2.20** | Needs `depends_on` conditions and `service_completed_successfully`. Compose **v1 (`docker-compose`) will not work.** |
| bash | 3.2 | Windows: use WSL2 or Git Bash |

Check yours:

```bash
./deployment/sh/dev.sh --version
```

### Installing Docker

**Linux (Debian/Ubuntu)**
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER      # then log out and back in
sudo systemctl enable --now docker
```

**Linux (Arch)**
```bash
sudo pacman -S docker docker-compose
sudo systemctl enable --now docker
sudo usermod -aG docker $USER      # then log out and back in
```

**macOS** — install Docker Desktop, or:
```bash
brew install --cask docker
```

**Windows** — install Docker Desktop with the WSL2 backend, then run
`./deployment/sh/dev.sh` **from inside WSL2 or Git Bash**. A `.sh` script
cannot run in `cmd.exe` or PowerShell.

---

## 3. What actually runs — 8 containers

| # | Service | Image | Port | RAM (idle) | Purpose |
|---|---|---|---|---|---|
| 1 | `conversation` | `yuviz-python:dev` | 50051 | **2.05 GB** | The STT→LLM→TTS pipeline. Holds whisper + kokoro in memory. gRPC. |
| 2 | `ollama` | `ollama/ollama` | 11434 | **2.01 GB** | LLM server, `llama3.2` resident |
| 3 | `admin-ui` | `yuviz-adminui:dev` | 3000 | **665 MB** | Next.js dev server, hot reload |
| 4 | `config` | `yuviz-python:dev` | 8000 | 52 MB | REST API — auth, tenants, agents, provider config |
| 5 | `knowledge` | `yuviz-python:dev` | 8100 | 50 MB | RAG documents, pgvector-backed |
| 6 | `postgres` | `pgvector/pgvector:pg14` | 5432 | 31 MB | Database + vector store |
| 7 | `webcall` | `yuviz-python:dev` | 8300 | 22 MB | Bridges the browser WebSocket to conversation's gRPC |
| 8 | `redis` | `redis:7-alpine` | 6379 | 3 MB | Cache and pub/sub |
| | | | **Total** | **≈ 4.9 GB** | |

Plus a 9th short-lived container, `init`, which applies schemas, creates the
admin user and seeds the default agent, then exits. Everything else waits for
it to finish successfully.

Three services account for 96% of the memory. The other five are nearly free.

**Four services share one image.** `config`, `knowledge`, `conversation` and
`webcall` all run from `yuviz-python:dev` with different `command:` entries —
they have identical dependencies, so four separate images would mean four
copies of PyTorch.

### Ports

`3000` admin UI · `5432` postgres · `6379` redis · `8000` config ·
`8100` knowledge · `8300` webcall · `11434` ollama · `50051` conversation

All eight bind to **127.0.0.1** by default. Postgres ships with default
credentials, and **redis, ollama and the conversation gRPC port have no
authentication at all** — no password exists to set. Anyone who can reach the
host on those ports has full read/write access.

`BIND_ADDR=0.0.0.0` is therefore not something a stronger password makes safe.
Only set it behind a private network, firewall, VPN or authenticated reverse
proxy that restricts who can reach these ports.

`dev.sh` checks the ports compose will publish, reading any `*_PORT` overrides
from `deployment/.env`. In the default containerized mode that is all eight;
with `USE_HOST_OLLAMA=1` it checks the seven compose ports and instead verifies
that a host Ollama is answering on 11434.

---

## 4. Disk usage

| Item | Size |
|---|---|
| `ollama/ollama` image | 6.27 GB |
| `yuviz-python:dev` image | 3.47 GB |
| `yuviz-adminui:dev` image | 2.29 GB |
| `pgvector/pgvector:pg14` image | 606 MB |
| `redis:7-alpine` image | 59 MB |
| Volume `ollama-models` (llama3.2) | 2.02 GB |
| Volume `hf-cache` (whisper + kokoro) | 815 MB |
| Volume `pgdata` | 54 MB |
| Volume `knowledge-docs` (uploads) | 0 B until used |
| **Total** | **≈ 15.6 GB** |

This is why `dev.sh` gates on 16 GB free.

Docker also accumulates **build cache** (tens of GB over time). Reclaim it with
`docker builder prune` — the next build is slower but nothing is lost.

Four named volumes persist across restarts, so `--down` never re-downloads
models. Only `--clean` deletes them.

---

## 5. What `dev.sh` does

Seven phases, each labelled so the script never looks hung:

```
[1/7] Checking Docker...      versions, RAM, free disk, port conflicts
[2/7] Creating .env...        generates secrets, backfills new keys
[3/7] Building containers...  docker compose up -d --build
[4/7] Downloading models...   llama3.2, whisper, kokoro (skips cached)
[5/7] Waiting for services... polls each healthcheck
[6/7] Running verification... proves STT, LLM and TTS actually work
[7/7] Ready!                  prints the URL and login
```

Phase 6 matters more than it looks. A healthcheck only proves a process is
listening — during development the Conversation Service reported healthy while
TTS was one missing model away from failing on the first reply. So phase 6
synthesises a sentence with TTS, transcribes that same audio back with STT, and
sends a prompt to the LLM. If any leg fails, the run fails.

### Flags

```bash
./deployment/sh/dev.sh                 # start and verify
./deployment/sh/dev.sh --down          # stop, keep all data
./deployment/sh/dev.sh --clean         # stop and delete volumes (full reset)
./deployment/sh/dev.sh --logs          # tail all logs
./deployment/sh/dev.sh --verbose       # full build/pull output
./deployment/sh/dev.sh --timeout 600   # slower machines or CI
./deployment/sh/dev.sh --version       # versions, for bug reports
```

Re-running is always safe. Existing models are skipped, `deployment/.env` is
never overwritten, and the database bootstrap is idempotent.

---

## 6. File layout

```
deployment/
├── .env.example                 committed template
├── .env                         generated, gitignored, holds secrets
├── docker/
│   ├── docker-compose.yml       8 services, healthchecks, ordering
│   ├── Dockerfile.python        shared image for the 4 Python services
│   └── Dockerfile.adminui       Next.js dev image
└── sh/
    ├── dev.sh                   entry point
    └── init.sh                  schemas → admin user → seed agent
```

`.dockerignore` stays at the repo root — Docker only reads it from the build
context root.

---

## 7. Configuration

Everything lives in `deployment/.env`, generated on first run.

| Variable | Default | Notes |
|---|---|---|
| `ADMIN_EMAIL` | `admin@yuviz.local` | Admin UI login |
| `ADMIN_PASSWORD` | `admin123` | Re-synced to the database every run — change it here and re-run |
| `CONFIG_SERVICE_PASSWORD` | random | Internal service account, not a UI login |
| `JWT_SECRET` | random | Signs Config Service tokens |
| `POSTGRES_DSN` | `postgresql://voiceai:voiceai@postgres:5432/voiceai` | Host is the compose service name |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Set automatically from `USE_HOST_OLLAMA` |
| `BIND_ADDR` | `127.0.0.1` | Host interface for every published port |
| `VOICEAI_STT_MODEL` | `small.en` | Used by both conversation and the seed — they must agree |
| `*_PORT` (×8) | see §3 | Override on collision |
| `SPACY_MODEL_URL` | empty | Mirror for the spaCy model if github.com is unreachable |

Three settings are pinned in `docker-compose.yml` instead, because a wrong
value breaks the stack **silently**:

- `VOICEAI_TTS_ENGINE=kokoro` — the code defaults to `macos`, which shells out
  to the macOS `say` binary and cannot work in a Linux container.
- `VOICEAI_TTS_VOICE=af_sarah` — the code defaults to `Samantha`, also a macOS
  voice. Kokoro returns 404 for it and the agent never speaks.
- `CONVERSATION_SVC_TARGET=conversation:50051` — webcall otherwise defaults to
  Envoy on `:10000`, which this stack does not run.

**Security note:** `admin123` ships in `.env.example`, so every clone has the
same password, and postgres defaults to `voiceai`/`voiceai`. Change
`ADMIN_PASSWORD`, `POSTGRES_PASSWORD` and `POSTGRES_DSN` before this stack
leaves your machine.

Credentials alone are not enough to expose it, though: redis, ollama and the
conversation gRPC port have no authentication to configure. Keep `BIND_ADDR` at
`127.0.0.1` unless a firewall, VPN or authenticated proxy controls access to
those ports.

---

## 8. Using a host Ollama (Mac GPU)

Containers cannot reach Apple Metal, so containerized Ollama runs the LLM on
CPU inside a VM — noticeably slow on a Mac. To use the host's Ollama instead:

```bash
ollama serve                       # in another terminal
USE_HOST_OLLAMA=1 ./deployment/sh/dev.sh
```

The stack then skips its own Ollama container and points every service at
`host.docker.internal:11434`. `dev.sh` fails early with a clear message if
nothing is listening there.

---

## 9. Troubleshooting

**"Compose 1.x is too old"** — you have `docker-compose` v1. This stack needs
Compose v2 (`docker compose`, no hyphen). Upgrade Docker.

**"only N GB free — need at least 10 GB"** — free space, or run
`docker system prune -a` (removes unused images from *all* projects) or
`docker builder prune` (build cache only, safer).

**"port NNNN in use"** — a local Postgres/Redis/dev server is running. Stop it,
or override that `*_PORT` in `deployment/.env`.

**A service never becomes healthy** — the error names the service and its log
command. `conversation` is the slowest by far; on a first run it loads whisper
and kokoro before serving. Retry with `--timeout 600`.

**Agent transcribes your speech but never replies** — TTS is failing. Check
`./deployment/sh/dev.sh --logs` for a 404 on a voice file; the engine and voice
settings must both be kokoro-compatible (see §7).

**Model download stalls or reports "outgoing traffic disabled"** — that is
`huggingface_hub`'s misleading message for a *connection failure*. Retry.
On IPv6-only networks, IPv4-only hosts (github.com, HF's CDN) need working
DNS64; test with `getent ahosts github.com`.

**Edits to `init.sh` seem ignored** — it is baked into the image via
`COPY . /app`. `dev.sh` always rebuilds, but a manual `docker compose up init`
does not.

**Login fails** — `dev.sh` re-syncs `ADMIN_PASSWORD` from `deployment/.env` on
every run, so re-running fixes a stale password.

---

## 10. Scope

**Included:** the browser-testing path — real STT → LLM → TTS calls from
`admin-ui`'s Test Agent panel, with barge-in.

**Not included:** real telephony. Kamailio SIP routing, FreeSWITCH media, the
C++ Gateway and the `campaigns`/`did`/`vobiz` services are not part of this
stack. Those are system-level installs with local config outside version
control — see the main [README](../README.md).

Also out of scope: GPU/CUDA support, production images (this runs `next dev`
with hot reload), and the knowledge ingestion worker (only the API is started).

For a native install without Docker, see [setup.md](setup.md) — more moving
parts, and only ever verified on macOS.
