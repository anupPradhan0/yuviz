#!/usr/bin/env bash
# One-command local dev stack.
#
#   ./deployment/sh/dev.sh              bring up and verify
#   ./deployment/sh/dev.sh --down       stop, keep data
#   ./deployment/sh/dev.sh --clean      stop and wipe volumes
#   ./deployment/sh/dev.sh --logs       tail logs
#   ./deployment/sh/dev.sh --verbose    full build output
#   ./deployment/sh/dev.sh --timeout N  health budget (default 300s)
#   ./deployment/sh/dev.sh --version    versions, for bug reports
#
# Docker is the only prerequisite. On Windows use WSL2 or Git Bash.
set -Eeuo pipefail

DEV_SH_VERSION="1.0.0"
MIN_DOCKER="20.10"
MIN_COMPOSE="2.20"
TOTAL_PHASES=7

TIMEOUT=300
VERBOSE=0
ACTION="up"
CURRENT_PHASE="startup"
STARTED_WORK=0

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE_FILE="$ROOT/deployment/docker/docker-compose.yml"
ENV_FILE="$ROOT/deployment/.env"
ENV_EXAMPLE="$ROOT/deployment/.env.example"
cd "$ROOT"

if [ -t 1 ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
    YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

phase()  { CURRENT_PHASE="$1/$TOTAL_PHASES $2"; printf '\n%s[%s/%s] %s%s\n' "$BOLD" "$1" "$TOTAL_PHASES" "$2" "$RESET"; }
ok()     { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
info()   { printf '  %s\n' "$1"; }
dim()    { printf '  %s%s%s\n' "$DIM" "$1" "$RESET"; }
warn()   { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
fail()   { printf '\n%s✗ %s%s\n' "$RED" "$1" "$RESET" >&2; }

run_quiet() {
    if [ "$VERBOSE" = "1" ]; then
        "$@"
    else
        local log; log=$(mktemp)
        if "$@" >"$log" 2>&1; then
            rm -f "$log"
        else
            local rc=$?
            tail -30 "$log" >&2
            rm -f "$log"
            return $rc
        fi
    fi
}

cleanup_hint() {
    cat >&2 <<EOF

  Logs:
    ./deployment/sh/dev.sh --logs

  Reset and start over:
    ./deployment/sh/dev.sh --clean && ./deployment/sh/dev.sh
EOF
}

on_err() {
    local rc=$?
    trap - ERR INT TERM
    fail "Startup failed at [$CURRENT_PHASE] (exit $rc)"
    [ "$STARTED_WORK" = "1" ] && cleanup_hint
    exit "$rc"
}

on_int() {
    trap - ERR INT TERM
    printf '\n\n%s^C Interrupted during [%s].%s\n' "$YELLOW" "$CURRENT_PHASE" "$RESET" >&2
    if [ "$STARTED_WORK" = "1" ]; then
        echo "   Containers left running; downloads are cached so re-running resumes." >&2
    fi
    exit 130
}

trap on_err ERR
trap on_int INT TERM

while [ $# -gt 0 ]; do
    case "$1" in
        --down)    ACTION="down" ;;
        --clean)   ACTION="clean" ;;
        --logs)    ACTION="logs" ;;
        --verbose) VERBOSE=1 ;;
        --timeout) TIMEOUT="${2:?--timeout needs a value in seconds}"; shift ;;
        --version) ACTION="version" ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) fail "unknown flag: $1 (try --help)"; exit 2 ;;
    esac
    shift
done

have_docker() { command -v docker >/dev/null 2>&1; }

compose() {
    local args=(-f "$COMPOSE_FILE" --project-directory "$ROOT")
    [ -f "$ENV_FILE" ] && args+=(--env-file "$ENV_FILE")
    docker compose "${args[@]}" "$@"
}

if [ "$ACTION" = "version" ]; then
    printf 'dev.sh          %s\n' "$DEV_SH_VERSION"
    printf 'requires        Docker Engine >= %s, Compose >= %s\n' "$MIN_DOCKER" "$MIN_COMPOSE"
    if have_docker; then
        printf 'docker          %s\n' "$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'not running')"
        printf 'docker compose  %s\n' "$(docker compose version --short 2>/dev/null || echo 'not found')"
    else
        printf 'docker          not installed\n'
    fi
    exit 0
fi

if ! have_docker; then
    fail "docker not found: https://docs.docker.com/get-docker/"
    exit 1
fi

case "$ACTION" in
    down)  info "Stopping (volumes kept)…"; compose --profile ollama-container down; ok "stopped"; exit 0 ;;
    clean) info "Stopping and deleting volumes…"; compose --profile ollama-container down -v; ok "cleaned"; exit 0 ;;
    logs)  exec compose --profile ollama-container logs -f --tail=100 ;;
esac

# ── [1/7] ─────────────────────────────────────────────────────────────────────
phase 1 "Checking Docker..."

version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

if ! docker info >/dev/null 2>&1; then
    fail "the Docker daemon is not running. Start Docker Desktop, or: sudo systemctl start docker"
    exit 1
fi

DOCKER_VER=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "0")
COMPOSE_VER=$(docker compose version --short 2>/dev/null || echo "")

if [ -z "$COMPOSE_VER" ]; then
    fail "Docker Compose v2 required — 'docker-compose' v1 cannot run this stack. Upgrade Docker."
    exit 1
fi
version_ge "$DOCKER_VER"  "$MIN_DOCKER"  || { fail "Docker $DOCKER_VER is too old (need >= $MIN_DOCKER)"; exit 1; }
version_ge "$COMPOSE_VER" "$MIN_COMPOSE" || { fail "Compose $COMPOSE_VER is too old (need >= $MIN_COMPOSE)"; exit 1; }

if [ "${USE_HOST_OLLAMA:-0}" = "1" ]; then
    OLLAMA_MODE="host"
    OLLAMA_URL="http://host.docker.internal:11434"
    COMPOSE_PROFILE=()
else
    OLLAMA_MODE="container"
    OLLAMA_URL="http://ollama:11434"
    COMPOSE_PROFILE=(--profile ollama-container)
fi

case "$(uname -s)" in
    Linux)  RAM_H=$(awk '/MemTotal/{printf "%.0f GB", $2/1048576}' /proc/meminfo 2>/dev/null || echo unknown) ;;
    Darwin) RAM_H=$(sysctl -n hw.memsize 2>/dev/null | awk '{printf "%.0f GB", $1/1073741824}' || echo unknown) ;;
    *)      RAM_H="unknown" ;;
esac

DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo "")
if [ -n "$DOCKER_ROOT" ] && [ -d "$DOCKER_ROOT" ]; then DF_TARGET="$DOCKER_ROOT"; else DF_TARGET="$ROOT"; fi
AVAIL_KB=$(df -Pk "$DF_TARGET" 2>/dev/null | awk 'NR==2{print $4}' || echo "")
if [ -n "$AVAIL_KB" ]; then AVAIL_GB=$(( AVAIL_KB / 1048576 )); DISK_H="${AVAIL_GB} GB"; else AVAIL_GB=""; DISK_H="unknown"; fi

printf '  %-11s %s\n' "Docker:"    "$DOCKER_VER"
printf '  %-11s %s\n' "Compose:"   "$COMPOSE_VER"
printf '  %-11s %s / %s\n' "OS/CPU:" "$(uname -s | tr '[:upper:]' '[:lower:]')" "$(uname -m)"
printf '  %-11s %s\n' "RAM:"       "$RAM_H"
printf '  %-11s %s\n' "Free disk:" "$DISK_H"
printf '  %-11s %s\n' "Ollama:"    "$OLLAMA_MODE"

if [ -n "$AVAIL_GB" ] && [ "$AVAIL_GB" -lt 16 ]; then
    fail "only ${AVAIL_GB} GB free on ${DF_TARGET} — need at least 16 GB"
    echo "    Reclaim some with: docker system prune -a" >&2
    exit 1
fi

if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
if [ -z "$(compose "${COMPOSE_PROFILE[@]}" ps -q 2>/dev/null)" ]; then
    conflict=0
    check_port() {
        if port_busy "$1"; then
            printf '  %s✗%s port %s in use — %s\n' "$RED" "$RESET" "$1" "$2" >&2
            conflict=1
        fi
    }
    check_port "${ADMIN_UI_PORT:-3000}"      "set ADMIN_UI_PORT in deployment/.env"
    check_port "${CONFIG_PORT:-8000}"        "set CONFIG_PORT"
    check_port "${KNOWLEDGE_PORT:-8100}"     "set KNOWLEDGE_PORT"
    check_port "${WEBCALL_PORT:-8300}"       "set WEBCALL_PORT"
    check_port "${CONVERSATION_PORT:-50051}" "set CONVERSATION_PORT"
    check_port "${POSTGRES_PORT:-5432}"      "a local Postgres is running; stop it or set POSTGRES_PORT"
    check_port "${REDIS_PORT:-6379}"         "a local Redis is running; stop it or set REDIS_PORT"
    [ "$OLLAMA_MODE" = "container" ] && check_port "${OLLAMA_PORT:-11434}" "a host Ollama is running; use USE_HOST_OLLAMA=1"
    if [ "$conflict" = "1" ]; then
        fail "free the ports above, or override them in deployment/.env"
        exit 1
    fi
    ok "all required ports free"
else
    dim "stack already running — skipping port check"
fi

# ── [2/7] ─────────────────────────────────────────────────────────────────────
phase 2 "Creating .env..."

# head must be the reader, not the writer: `tr < /dev/urandom | head -c N` kills
# tr with SIGPIPE (exit 141) the moment head has enough, which trips the ERR trap.
rand() { head -c "$(( ${1:-32} * 3 ))" /dev/urandom | base64 | LC_ALL=C tr -cd 'A-Za-z0-9' | cut -c "1-${1:-32}"; }

if [ -f "$ENV_FILE" ]; then
    ok "deployment/.env exists (left untouched)"
    # Backfill keys added to .env.example since this .env was generated,
    # otherwise compose warns about unset variables after an update.
    while IFS='=' read -r key _; do
        case "$key" in ''|\#*) continue ;; esac
        grep -q "^${key}=" "$ENV_FILE" || {
            grep "^${key}=" "$ENV_EXAMPLE" >> "$ENV_FILE"
            ok "added missing key ${key}"
        }
    done < "$ENV_EXAMPLE"
else
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    ok "deployment/.env created"
fi

# Fill any secret that is missing or blank. This has to run for both paths:
# .env.example ships these keys empty, so the backfill above would otherwise
# copy an empty value in. An empty JWT_SECRET is silently accepted by
# services/config/auth.py (os.environ.get returns "", so its insecure-default
# fallback never fires) and becomes the actual signing key.
for secret in CONFIG_SERVICE_PASSWORD:32 JWT_SECRET:48; do
    key=${secret%:*}; len=${secret#*:}
    if [ -z "$(grep "^${key}=" "$ENV_FILE" | cut -d= -f2-)" ]; then
        sed -i.bak "s|^${key}=.*|${key}=$(rand "$len")|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
        ok "generated ${key}"
    fi
done

# Re-derived each run so toggling USE_HOST_OLLAMA needs no hand-editing.
sed -i.bak "s|^OLLAMA_BASE_URL=.*|OLLAMA_BASE_URL=${OLLAMA_URL}|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
ok "ollama URL: ${OLLAMA_URL}"

if [ "$OLLAMA_MODE" = "host" ] && ! curl -fsS -m 5 http://localhost:11434/api/tags >/dev/null 2>&1; then
    fail "USE_HOST_OLLAMA=1 but nothing answers on localhost:11434 — run: ollama serve"
    exit 1
fi

# ── [3/7] ─────────────────────────────────────────────────────────────────────
phase 3 "Building containers..."
STARTED_WORK=1
dim "first run installs Python deps — several minutes"
run_quiet compose "${COMPOSE_PROFILE[@]}" up -d --build
ok "containers started"

# ── [4/7] ─────────────────────────────────────────────────────────────────────
phase 4 "Downloading models..."

if [ "$OLLAMA_MODE" = "container" ]; then
    deadline=$(( $(date +%s) + TIMEOUT ))
    until compose "${COMPOSE_PROFILE[@]}" exec -T ollama ollama list >/dev/null 2>&1; do
        [ "$(date +%s)" -ge "$deadline" ] && { fail "ollama did not come up within ${TIMEOUT}s"; cleanup_hint; exit 1; }
        sleep 3
    done
    if compose "${COMPOSE_PROFILE[@]}" exec -T ollama ollama list 2>/dev/null | grep -q 'llama3.2'; then
        ok "llama3.2 already present, skipping"
    else
        dim "pulling llama3.2 (~2 GB, first run only)"
        run_quiet compose "${COMPOSE_PROFILE[@]}" exec -T ollama ollama pull llama3.2
        ok "llama3.2 pulled"
    fi
else
    if curl -fsS -m 10 http://localhost:11434/api/tags 2>/dev/null | grep -q 'llama3.2'; then
        ok "llama3.2 already present on host, skipping"
    else
        dim "pulling llama3.2 on the host (~2 GB)"
        ollama pull llama3.2
        ok "llama3.2 pulled"
    fi
fi

# Test for the weight files, not the directory: an interrupted download leaves
# the folder behind with only metadata in it, and treating that as "cached"
# skips the real download and fails later with IncompleteSnapshotError.
cached() { compose "${COMPOSE_PROFILE[@]}" exec -T conversation sh -c "ls $1 >/dev/null 2>&1" 2>/dev/null; }

if cached '/root/.cache/huggingface/hub/models--hexgrad--Kokoro-82M/snapshots/*/kokoro-v1_0.pth'; then
    ok "kokoro weights cached, skipping"
else
    dim "kokoro weights (~313 MB) download during startup"
fi

# Pull whisper here rather than letting it happen lazily at first transcribe:
# a flaky network then surfaces as a confusing verification failure, and
# huggingface_hub reports connection errors as "outgoing traffic disabled".
if cached '/root/.cache/huggingface/hub/models--*faster-whisper*/snapshots/*/model.bin'; then
    ok "whisper weights cached, skipping"
else
    dim "pulling whisper (~500 MB, first run only)"
    whisper_pulled=0
    for _ in 1 2 3; do
        if run_quiet compose "${COMPOSE_PROFILE[@]}" exec -T conversation python -c \
            "import os;from faster_whisper.utils import download_model;download_model(os.environ.get('VOICEAI_STT_MODEL','small.en'))"; then
            whisper_pulled=1; break
        fi
        dim "download interrupted, retrying"
    done
    [ "$whisper_pulled" = "1" ] || { fail "whisper model download failed after 3 attempts (network?)"; cleanup_hint; exit 1; }
    ok "whisper pulled"
fi

# ── [5/7] ─────────────────────────────────────────────────────────────────────
phase 5 "Waiting for services..."

wait_healthy() {
    local svc="$1" deadline=$(( $(date +%s) + TIMEOUT )) cid status
    while :; do
        cid=$(compose "${COMPOSE_PROFILE[@]}" ps -q "$svc" 2>/dev/null || true)
        if [ -n "$cid" ]; then
            status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo "")
            case "$status" in
                healthy|running) [ "$status" = "healthy" ] && { ok "$svc healthy"; return 0; } ;;
                exited|dead)
                    fail "$svc exited during startup"
                    echo "    docker compose -f $COMPOSE_FILE logs $svc" >&2
                    return 1 ;;
            esac
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            fail "$svc failed to become healthy after ${TIMEOUT}s"
            echo "    docker compose -f $COMPOSE_FILE logs $svc" >&2
            echo "    slow machine or first-run download? retry with: --timeout $(( TIMEOUT * 2 ))" >&2
            return 1
        fi
        sleep 5
    done
}

dim "conversation loads whisper + kokoro before serving — slowest on first run"
for svc in postgres redis config knowledge conversation webcall admin-ui; do
    wait_healthy "$svc" || { cleanup_hint; exit 1; }
done

# ── [6/7] ─────────────────────────────────────────────────────────────────────
phase 6 "Running verification..."

# Health endpoints only prove a process is listening. Exercise all three legs:
# TTS makes audio, STT reads that same audio back, the LLM answers a prompt.
VERIFY_OUT=$(compose "${COMPOSE_PROFILE[@]}" exec -T conversation python - <<'PY' 2>&1
import asyncio, os, sys

async def main():
    from services.conversation.providers.tts.kokoro import KokoroTTS
    from services.conversation.providers.stt.faster_whisper import FasterWhisperSTT
    import httpx

    phrase = "The quick brown fox jumps over the lazy dog."

    # Use the configured voice, not a hardcoded one: a voice the engine cannot
    # load leaves the agent permanently silent, and hardcoding hides exactly that.
    tts = KokoroTTS(voice=os.environ.get("VOICEAI_TTS_VOICE", "af_heart"), speed=1.0)
    pcm = b"".join([c async for c in tts.synthesize_stream(phrase, 16000)])
    if not pcm:
        print("FAIL tts produced no audio"); sys.exit(1)
    print(f"OK tts {len(pcm)} bytes ({len(pcm)/2/16000:.1f}s)")

    stt = FasterWhisperSTT(model_size=os.environ.get("VOICEAI_STT_MODEL", "small.en"))
    await stt.load()
    res = await stt.transcribe(pcm, 16000)
    text = (getattr(res, "text", "") or "").strip()
    if not text:
        print("FAIL stt returned an empty transcript"); sys.exit(1)
    print(f"OK stt {text!r}")

    url = os.environ.get("VOICEAI_LLM_URL", "http://ollama:11434").rstrip("/")
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(f"{url}/api/generate", json={
            "model": "llama3.2", "prompt": "Say hello in three words.", "stream": False})
        r.raise_for_status()
        out = (r.json().get("response") or "").strip()
    if not out:
        print("FAIL llm returned an empty completion"); sys.exit(1)
    print(f"OK llm {out[:60]!r}")

asyncio.run(main())
PY
) || {
    fail "pipeline verification failed"
    printf '%s\n' "$VERIFY_OUT" | sed 's/^/    /' >&2
    cleanup_hint
    exit 1
}

# ${leg^^} would be cleaner but macOS still ships bash 3.2.
printf '%s\n' "$VERIFY_OUT" | grep -E '^OK ' | while read -r _ leg rest; do
    ok "$(printf '%s' "$leg" | tr '[:lower:]' '[:upper:]'): ${rest}"
done

SEEDED_URL=$(compose "${COMPOSE_PROFILE[@]}" exec -T postgres \
    psql -U "${POSTGRES_USER:-voiceai}" -d "${POSTGRES_DB:-voiceai}" -tAc \
    "select extra->>'base_url' from provider_configs where engine='ollama' limit 1" 2>/dev/null | tr -d '[:space:]' || echo "")
if [ -n "$SEEDED_URL" ] && [ "$SEEDED_URL" != "$OLLAMA_URL" ]; then
    warn "seeded LLM url is '$SEEDED_URL' but this run uses '$OLLAMA_URL' — reseed with --clean"
else
    ok "database seeded (llm url: ${SEEDED_URL:-$OLLAMA_URL})"
fi

# ── [7/7] ─────────────────────────────────────────────────────────────────────
phase 7 "Ready!"
ADMIN_EMAIL_V=$(grep '^ADMIN_EMAIL=' "$ENV_FILE" | cut -d= -f2-)
ADMIN_PASSWORD_V=$(grep '^ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)
cat <<EOF

  ${GREEN}✓${RESET} All services healthy
  ${GREEN}✓${RESET} Models ready
  ${GREEN}✓${RESET} Database seeded
  ${GREEN}✓${RESET} STT verified
  ${GREEN}✓${RESET} LLM verified
  ${GREEN}✓${RESET} TTS verified

  ${BOLD}Open: http://localhost:3000${RESET}
  Login:  ${ADMIN_EMAIL_V}  /  ${ADMIN_PASSWORD_V}
  Then select the "default" agent → Click "Test Agent"

  ${DIM}./deployment/sh/dev.sh --logs    tail logs
  ./deployment/sh/dev.sh --down    stop
  ./deployment/sh/dev.sh --clean   stop and wipe data${RESET}

EOF
