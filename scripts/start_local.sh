#!/usr/bin/env bash
# start_local.sh — Starts the full native macOS Voice AI stack.
# Run each block in a SEPARATE terminal tab, in order (mysql -> kamailio ->
# freeswitch is a hard dependency chain: kamailio's dispatcher/routing
# tables live in MySQL, and FreeSWITCH registers with Kamailio as its
# upstream SIP proxy).
#
# Prerequisites (already installed):
#   MySQL, Kamailio, FreeSWITCH, Redis, PostgreSQL, Ollama, faster-whisper
#
# Usage: source this file to get helper functions, or copy individual blocks.

set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

# ── Block 1: MySQL — Kamailio's dispatcher/routing tables live here ─────────
start_mysql() {
  brew services start mysql 2>/dev/null || true
  echo "✓ MySQL running (kamailio database)"
}

# ── Block 2: Kamailio — SIP proxy, must be up before FreeSWITCH registers ───
start_kamailio() {
  kamailio -f /usr/local/etc/kamailio/kamailio.cfg -D -E
}

# ── Block 3: Data layer (Postgres/Redis; run once, may already be running) ──
start_data() {
  brew services start postgresql@14 2>/dev/null || true
  brew services start redis         2>/dev/null || true
  psql voiceai -f "$REPO/database/schema.sql" 2>/dev/null || \
    echo "schema already applied or voiceai db missing — run: psql postgres -c 'CREATE DATABASE voiceai;'"
  psql voiceai -f "$REPO/database/knowledge_schema.sql" 2>/dev/null || true
  echo "✓ PostgreSQL + Redis running"
}

# ── Block 4: Ollama — local LLM + embedding provider ─────────────────────────
start_ollama() {
  ollama serve
}

# ── Block 5: Config Service (REST API, port 8000) ────────────────────────────
start_config_service() {
  export POSTGRES_DSN="postgresql://satish@localhost:5432/voiceai"
  cd "$REPO"
  python3 -m uvicorn services.config.app:app --host 0.0.0.0 --port 8000
}

# ── Block 6: Knowledge Service (REST API, port 8100) ──────────────────────────
start_knowledge_service() {
  export POSTGRES_DSN="postgresql://satish@localhost:5432/voiceai"
  export REDIS_URL="redis://localhost:6379/0"
  export KNOWLEDGE_STORAGE_ROOT="$REPO/data/knowledge_documents"
  cd "$REPO"
  python3 -m uvicorn services.knowledge.app:app --host 0.0.0.0 --port 8100
}

# ── Block 7: Knowledge ingestion worker (background job-queue poller) ────────
start_knowledge_worker() {
  export POSTGRES_DSN="postgresql://satish@localhost:5432/voiceai"
  export KNOWLEDGE_STORAGE_ROOT="$REPO/data/knowledge_documents"
  cd "$REPO"
  python3 -m services.knowledge --log-level INFO
}

# ── Block 8/9: Python ConversationService instances (ports 50051/50052) ──────
_conv_env() {
  export POSTGRES_DSN="postgresql://satish@localhost:5432/voiceai"
  export REDIS_URL="redis://localhost:6379/0"
  export CONFIG_SERVICE_URL="http://localhost:8000"
  export CONFIG_SERVICE_EMAIL="conversation-service@internal.yuviz.ai"
  export CONFIG_SERVICE_PASSWORD="${CONFIG_SERVICE_PASSWORD:?set this in your shell — see docs/setup.md §4, never commit the real value}"
  export KNOWLEDGE_SERVICE_URL="http://localhost:8100"
}
start_conv1() {
  _conv_env
  cd "$REPO"
  python3 -m services.conversation --port 50051 --mode pipeline --log-level INFO
}
start_conv2() {
  _conv_env
  cd "$REPO"
  python3 -m services.conversation --port 50052 --mode pipeline --log-level INFO
}

# ── Block 10: Envoy gRPC proxy ────────────────────────────────────────────────
start_envoy() {
  # Install func-e if missing: brew install func-e
  func-e run -c "$REPO/config/envoy.yaml"
}

# ── Block 11: C++ Gateway ──────────────────────────────────────────────────────
start_gateway() {
  cd "$REPO"
  ./build/gateway/voice_ai_gateway config/gateway.yaml
}

# ── Block 12: FreeSWITCH (registers with Kamailio from Block 2) ──────────────
start_freeswitch() {
  cd "$REPO"
  ./freeswitch
}

# ── Block 13: Admin UI (Next.js, port 3000) ───────────────────────────────────
start_admin_ui() {
  cd "$REPO/admin-ui"
  npm run dev
}

# ── Verify: check all services are healthy ───────────────────────────────────
verify() {
  echo "=== Port check ==="
  for port in 3306 5060 5080 5432 6379 11434 8000 8100 50051 50052 10000 8080 3000; do
    nc -z localhost "$port" 2>/dev/null && echo "  :$port  OPEN" || echo "  :$port  CLOSED"
  done

  echo ""
  echo "=== Config / Knowledge Service health ==="
  curl -s http://localhost:8000/health || echo "  Config Service not reachable"
  echo ""
  curl -s http://localhost:8100/health || echo "  Knowledge Service not reachable"

  echo ""
  echo "=== Envoy upstream health ==="
  curl -s http://localhost:9901/clusters | grep conversation_svc | grep health || \
    echo "  Envoy admin not reachable — is Envoy running?"

  echo ""
  echo "=== Recent call records ==="
  psql voiceai -c "SELECT session_id, duration_ms, turn_count, close_reason FROM calls ORDER BY started_at DESC LIMIT 5;" 2>/dev/null || \
    echo "  (no call records yet)"
}

# ── Port map ─────────────────────────────────────────────────────────────────
portmap() {
  cat <<'EOF'
  :3306   MySQL      — Kamailio dispatcher/routing tables
  :5060   Kamailio   — SIP proxy (start before FreeSWITCH)
  :5080   FreeSWITCH — SIP UA (registers with Kamailio)
  :6379   Redis      — session state + config cache-aside
  :5432   PostgreSQL — CDR, transcripts, config, knowledge base
  :11434  Ollama     — local LLM + embedding provider
  :8000   Config Service    — REST API
  :8100   Knowledge Service — REST API (RAG)
  :50051  ConvSvc-1  — gRPC ConversationService
  :50052  ConvSvc-2  — gRPC ConversationService
  :10000  Envoy      — gRPC load balancer (upstream -> 50051, 50052)
  :9901   Envoy admin — http://localhost:9901
  :8080   C++ Gateway — WebSocket (FreeSWITCH mod_audio_fork -> here)
  :9090   Gateway metrics — http://localhost:9090/metrics
  :3000   Admin UI   — Next.js
EOF
}

echo "start_local.sh loaded. Functions: start_mysql, start_kamailio, start_data, start_ollama, start_config_service, start_knowledge_service, start_knowledge_worker, start_conv1, start_conv2, start_envoy, start_gateway, start_freeswitch, start_admin_ui, verify, portmap"
