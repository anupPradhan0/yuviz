# Setup — Web-Testing Path (Fork & Run)

> **Most people should run `./deployment/sh/dev.sh` instead** — one command, Docker the only
> prerequisite, works on macOS/Linux/Windows (PowerShell, Command Prompt,
> Git Bash or WSL2). See the
> Quickstart in [README.md](../README.md). This document covers the **native**
> install, which is useful for debugging a service directly against your own
> Python environment but has more moving parts and was only verified on macOS.

Status: **built and verified** (2026-08-04). Covers the subset of this
platform that a fresh fork can actually run: the Config Service, Knowledge
Service, Conversation Service (STT/LLM/TTS pipeline), the webcall bridge,
and admin-ui's "Test Agent" browser panel. This deliberately excludes
native SIP/telephony (Kamailio, FreeSWITCH, the C++ Gateway, MySQL) — that
infra lives outside this repo (system packages + unversioned local config)
and isn't reproducible by a fork; see `scripts/start_local.sh` if you have
that stack already installed. Everything below was verified end-to-end
against a genuinely fresh venv and a fresh database on macOS, not just the
long-lived dev environment — but only on macOS. This path has been
audited for macOS-only code (none found in the required default
providers — `faster_whisper`/`ollama`/`kokoro` are all cross-platform,
and the one macOS-only TTS provider is opt-in, not on by default) but has
not actually been run on Linux. If you hit a Linux-specific issue,
please report it rather than assuming it's expected.

## 1. Prerequisites

- **PostgreSQL** (14+) and **Redis** — the only required data stores.
- **Python 3.11** — this codebase has only ever run on 3.11; nothing here
  has been tested on 3.12+.
- **Node.js** — for admin-ui (Next.js).
- **Ollama** — only if you want fully local STT/LLM/TTS with no API keys
  (the seed script's default: `faster_whisper` + `llama3.2` + `kokoro`). If
  using Ollama, also pull the model before starting the stack — the seed
  script only registers `llama3.2` as the tenant's default, it doesn't
  download it: `ollama pull llama3.2`. Skip both if you'd rather wire in a
  cloud provider (Deepgram, Gemini,
  ElevenLabs — see `services/conversation/providers/`) through the admin
  UI after setup.

On macOS with Homebrew: `brew install postgresql@14 redis node ollama`.

## 2. Python environment

```bash
cd voice-ai-platform
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt
```

`requirements.txt` is a `pip freeze`-generated, exact-pinned list verified
clean in three separate from-scratch venvs — see its header comment for
the two packages it deliberately pins past their latest-yanked versions
(`grpcio`/`grpcio-tools`/`grpcio-health-checking`, `charset-normalizer`).

## 3. VAD model

`libs/vad_sdk/silero_vad.py` loads `models/silero_vad.onnx`, which isn't
committed to the repo (binary model weights). It's confirmed identical to
the copy `faster-whisper` already bundles as a pip dependency, so just
copy it out instead of downloading anything separately:

```bash
mkdir -p models
cp venv/lib/python3.11/site-packages/faster_whisper/assets/silero_vad_v6.onnx models/silero_vad.onnx
```

## 4. Database

```bash
brew services start postgresql@14
brew services start redis
createdb voiceai
psql voiceai -f database/schema.sql
psql voiceai -f database/knowledge_schema.sql
psql voiceai -f database/telephony_schema.sql
```

`schema.sql` seeds a `default` tenant row — the seed script in step 6
depends on it existing.

### Environment variables — full reference

Each service that reads env vars has a `.env.example` next to its code
(`services/config/.env.example`, `services/knowledge/.env.example`,
`services/conversation/.env.example`, `services/webcall/.env.example`)
listing every variable it reads, with a placeholder value and a one-line
explanation. `scripts/start_web_test.sh` already exports sane localhost
defaults for all of these except `CONFIG_SERVICE_PASSWORD` (step 5 below),
so you don't need to manually set anything to follow this doc — the
`.env.example` files are there for when you run a service directly
(`cp services/X/.env.example services/X/.env`, then `set -a; source
services/X/.env; set +a`) instead of through the helper script, e.g. in a
container or on a remote host.

## 5. Service-account credentials

The Conversation Service (and Knowledge Service) authenticate to the
Config Service's REST API as a real user, not a shared secret baked into
this repo. Create one yourself and export it — never commit the real
value:

```bash
export POSTGRES_DSN="postgresql://$(whoami)@localhost:5432/voiceai"
./venv/bin/python3 scripts/create_service_account.py conversation-service@internal.yuviz.ai '<pick-your-own-password>'
export CONFIG_SERVICE_PASSWORD='<the-same-password>'
```

`scripts/start_web_test.sh` reads `CONFIG_SERVICE_PASSWORD` from your
shell and fails loudly if it's unset, rather than falling back to a
hardcoded default.

Provider credentials pasted into the Admin UI (or stored via
`api_key`/`secondary_api_key_ref` on a provider/tool config) are encrypted
at rest with the `enc:` scheme (`libs/config_sdk/secrets.py`) — both the
Config Service and every Conversation Service instance need the same
Fernet key to encrypt/decrypt them:

```bash
./venv/bin/python3 -c "from libs.config_sdk.secrets import generate_key; print(generate_key())"
export SECRET_ENCRYPTION_KEY='<the-generated-value>'
```

Generate this once and keep it — losing it means every `enc:`-stored
credential becomes permanently undecryptable, not just hard to find.
`scripts/start_local.sh`'s `start_config_service()`/`_conv_env()` both
fail loudly if it's unset, same posture as `CONFIG_SERVICE_PASSWORD`.

`JWT_SECRET` signs the login tokens. There is no hardcoded fallback — a
default in a public repo would be the signing key, letting anyone forge a
superadmin token — so `services/config/auth.py` refuses to start without
it, and Config/Knowledge/Campaigns (which all verify tokens minted by
Config) must share the same value:

```bash
export JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
```

## 6. Seed a default agent

```bash
export REDIS_URL="redis://localhost:6379/0"
./venv/bin/python3 scripts/seed_default_config.py
```

Idempotent — safe to re-run. Creates `stt/faster_whisper`,
`llm/ollama`, and `tts/kokoro` provider_configs plus a `default` agent
under the `default` tenant, matching `config/agents/default.yaml`'s
content so a local test call sounds the same as before this config moved
into the database.

## 7. Start the stack

```bash
source scripts/start_web_test.sh
```

This loads shell functions rather than starting everything at once — run
each in its own terminal tab, in this order:

1. `start_data` (if you skipped step 4 above manually)
2. `start_ollama` (only if using local models)
3. `start_config_service`
4. `start_knowledge_service` (optional — only needed for RAG-backed agents)
5. `start_conv1`
6. `start_webcall`
7. `start_admin_ui`

Then, from a fresh tab, run `./scripts/verify_setup.sh` — unlike the
`verify` shell function (a quick port-only check), this confirms each
service actually reports healthy: it calls Config Service's and
Knowledge Service's `/health` endpoints and makes a real gRPC health
check against Conversation Service, not just a TCP connect.

## 8. Test an agent

Open `http://localhost:3000`. On a database that has no superadmin yet the
login page opens on **Create your administrator account** — enter your own
email and a password of at least 8 characters (minimum enforced by
`services/config/schemas.py`), and you are signed in as the first superadmin.
The form reverts to plain sign-in for good once that account exists; `scripts/
create_superadmin.py` is the headless equivalent if you would rather not use
the browser.

Then navigate to the `default` agent and click
**Test Agent** — this opens `admin-ui/components/TestAgentPanel.tsx`,
which talks directly to the webcall bridge over WebSocket and does its own
in-browser VAD (calibrated noise floor, barge-in support). Speak into your
mic; you should hear the agent's greeting and get real STT → LLM → TTS
turns with working interruption.

## 9. What's excluded here, and why

Real phone calls (Kamailio SIP routing, FreeSWITCH media, the C++
Gateway's `CallFSM`, the Vobiz PSTN bridge) are not reproducible from this
repo alone — Kamailio and FreeSWITCH are system-level installs with local
config outside version control, and the Gateway is a compiled native
service with its own build toolchain. If you have that stack already
running, `scripts/start_local.sh` covers it instead of this doc. Adding a
containerized/reproducible telephony stack is tracked as future work, not
started.
