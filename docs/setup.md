# Setup — Web-Testing Path (Fork & Run)

> **Most people should run `./deployment/sh/dev.sh` instead** — one command, Docker the only
> prerequisite, works on macOS/Linux/Windows (PowerShell, Command Prompt,
> Git Bash or WSL2). See the
> Quickstart in [README.md](../README.md). This document covers the **native**
> install, which is useful for debugging a service directly against your own
> Python environment but has more moving parts and was only verified on macOS.

Status: **built and verified**. Covers the subset of this
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
psql voiceai -v ON_ERROR_STOP=1 -f database/schema.sql
psql voiceai -f database/knowledge_schema.sql
psql voiceai -f database/telephony_schema.sql
```

`schema.sql` seeds a `default` tenant row — the seed script in step 6
depends on it existing.

### Environment variables — one `.env`

Every setting lives in the repo's `.env` (gitignored). `.env.example` lists
all of them, grouped and commented. Both launchers, `scripts/start_web_test.sh`
(this guide) and `scripts/start_local.sh` (full telephony stack), load `.env`
when sourced:

- On first run it copies `.env.example` to `.env`, and it adds any key added
  to the example later.
- It generates the blank platform secrets: `JWT_SECRET`,
  `SECRET_ENCRYPTION_KEY`, `CONFIG_SERVICE_PASSWORD` and `YUVIZ_APP_PASSWORD`.
  It never changes a value you already have.
- Each `start_*` block refuses to start, naming the key, if a setting it
  needs is blank.

A variable already set in your shell wins over `.env`, so you can override
one value for a single run. Docker uses `deployment/.env` instead
(`deployment/sh/dev.sh` generates it), because its hosts are container names.

Fill these yourself; nothing can generate them:

| Key | What |
|---|---|
| `FREESWITCH_ESL_PASSWORD` | Must match `password` in FreeSWITCH's `event_socket.conf.xml`. The Gateway and Campaigns refuse to use ESL without it. |
| `KAMAILIO_DB_URL` | Kamailio's MySQL, e.g. `mysql://kamailio:<password>@localhost/kamailio`. Used by `scripts/update_kamailio_ip.sh`. |
| `TELEPHONY_PUBLIC_BASE_URL` | Public tunnel URL for Vobiz/Cloudonix callbacks. |
| `SMTP_*`, `GOOGLE_*`, provider API keys | Optional features. |

`SIP_PROXY_HOST` ships blank: `update_kamailio_ip.sh` writes the IP Kamailio listens on. Until it does, the Gateway and Campaigns refuse to dial numbers.

## 5. Service-account credentials

The Conversation Service authenticates to Config as a real user. Create it
with the password the launcher generated into `.env` (sourcing it creates
`.env` on first run and loads it). Run it in a `( ... )` subshell: the
launcher turns on `set -euo pipefail`, and this one-off command fails on a
re-run (the account already exists), which would otherwise close your
terminal:

```bash
( source scripts/start_web_test.sh &&
  ./venv/bin/python3 scripts/create_service_account.py "$CONFIG_SERVICE_EMAIL" "$CONFIG_SERVICE_PASSWORD" )
```

`SECRET_ENCRYPTION_KEY` encrypts provider credentials pasted into the Admin
UI (`enc:` scheme, `libs/config_sdk/secrets.py`). Keep it: losing it makes
every stored `enc:` credential permanently undecryptable. `JWT_SECRET` signs
login tokens and is shared by every service through `.env`.

Invite emails (user onboarding) are sent via stdlib `smtplib` — no new
dependency, no queue. `SMTP_PASSWORD_REF` follows the same `env:`/`k8s:`
secret-ref convention as `api_key_ref`/`auth_token_ref` above; the rest are
plain config, not secrets:

Set them in `.env`:

```bash
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=invites@example.com
SMTP_FROM=invites@example.com
SMTP_PASSWORD_REF=env:SMTP_PASSWORD
SMTP_PASSWORD=abcd efgh ijkl mnop
SMTP_STARTTLS=true
INVITE_BASE_URL=http://localhost:3000
```

Values are read literally, up to the end of the line: no quotes, and spaces
(as in a Gmail app password) are fine.

`INVITE_BASE_URL` is the Admin UI origin the accept link points at —
`${INVITE_BASE_URL}/invite#<token>`, token in the fragment so it never
reaches a server log. An SMTP send failure doesn't fail the invite
request: the invite is still created `pending` and the response reports
`email_sent: false`; resend tries again.

`SMTP_USER` is optional — leave it unset for a relay that needs no
authentication (a local dev sink, or an internal relay that authorises by
source IP); `SMTP_PASSWORD_REF` is only required when `SMTP_USER` is set.

`SMTP_STARTTLS` defaults to `true` and upgrades the connection with
STARTTLS before authenticating — both `SMTP_PASSWORD` and the invite token
itself are bearer-equivalent, so sending either over a cleartext
connection on port 587 is a real credential leak, not just a lint issue.
It must stay `true` everywhere except local development. Set it to
`false` only against a local dev relay with no TLS support at all (e.g.
MailHog, `aiosmtpd` on port 1025) — a relay that refuses STARTTLS is
treated as a send failure (same `email_sent: false` path above), never a
silent fallback to cleartext. The upgrade verifies the relay's certificate
and hostname (no way to disable that while `SMTP_STARTTLS=true`), so the
relay needs a valid, trusted certificate — a self-signed or expired one
will fail the send the same way a refused STARTTLS does.

### Tool Execution Service (custom API chains, port 8600)

`services/toolexec/` (started by `scripts/start_local.sh`'s
`start_toolexec_service()`) owns the custom-API registry and runs
`execute_api` chains on the Conversation Service's behalf. No new
service-account is needed: `services/conversation/tools/providers/toolexec/
client.py` (`ToolExecClient`) authenticates to Config Service's
`/auth/login` with the SAME `conversation-service@internal.yuviz.ai`
account created in step 5 above, reusing `CONFIG_SERVICE_EMAIL`/
`CONFIG_SERVICE_PASSWORD`/`CONFIG_SERVICE_URL` — nothing new to create
there. Its own env vars:

```bash
export TOOLEXEC_SERVICE_URL="http://localhost:8600"        # Conversation Service's base URL for it; default shown
export TOOLEXEC_EXECUTE_SUBJECTS="conversation-service@internal.yuviz.ai"  # comma-separated emails allowed to POST /internal/chains/execute; default shown
export TOOLEXEC_TENANT_SECRET_ROOT="/path/to/tenant/secrets"  # where tenant-namespaced enc:/env:/k8s: refs resolve — NEVER the platform k8s secret mount; required, no default
export TOOLEXEC_HTTP_HOST_ALLOWLIST=""                      # comma-separated hostnames allowed to use http:// or a non-default port instead of https://; empty = https-only, no exceptions
export TOOLEXEC_ARGS_HMAC_KEY_REF="env:TOOLEXEC_HMAC_KEY"   # platform secret ref (CompositeSecretResolver) — required, no default; see rotation note below
export TOOLEXEC_ARGS_HMAC_KEY_ID="k1"                       # key id embedded in every derived hash/idempotency-key, so a rotation is visible in stored data; default shown
export TOOLEXEC_SIDE_EFFECT_CLAIM_TTL="24 hours"            # how long a claimed side-effecting step blocks a retry of the same arguments; default shown
export TOOLEXEC_MAX_CHAIN_BUDGET_MS="30000"                 # hard ceiling on any chain's whole-chain wall clock, regardless of what the caller/agent policy requests; default shown
export TOOLEXEC_MAX_CONCURRENT_RUNS_PER_AGENT="4"           # abuse brake, per (tenant_id, agent_id), per service replica; default shown
export TOOLEXEC_MAX_RUNS_PER_MINUTE_PER_AGENT="60"          # same scope as above; default shown
export TOOLEXEC_MAX_RESPONSE_BYTES="1048576"                # a step's upstream response is abandoned past this many bytes (1 MiB); default shown
```

`TOOLEXEC_ARGS_HMAC_KEY_REF` rotation: `_derive()` (`services/toolexec/
executor.py`) uses this one key, with a different domain-separation tag,
for BOTH the platform's own side-effect claim (`arguments_hash`, stored in
`api_chain_steps`/`api_side_effect_claims`) AND the value sent to the
downstream API in its own idempotency header
(`custom_apis.idempotency_header`). Rotating the key therefore blinds
**both** of those at once — the platform's fail-closed retry guard and the
downstream API's own dedupe — for one `TOOLEXEC_SIDE_EFFECT_CLAIM_TTL`
window, since both are derived from the same rotated secret. Plan a
rotation around that window, not around the platform claim alone.

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

`start_web_test.sh` deliberately does not start Tool Execution Service —
only `scripts/start_local.sh` (the full native stack, see §9) does, via
its own `start_toolexec_service()`. Add it to your tab order between
`start_knowledge_service` and `start_conv1` if you're testing `execute_api`
chains locally; `start_local.sh`'s own `verify`/`portmap` already cover
its `:8600` port and `/health` check.

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
