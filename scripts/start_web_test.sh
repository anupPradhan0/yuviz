#!/usr/bin/env bash
# start_web_test.sh — Starts just enough of the stack to test an agent
# through the browser (webcall bridge + admin-ui's Test Agent panel), with
# no telephony at all: no Kamailio, no FreeSWITCH, no MySQL, no C++
# Gateway. See start_local.sh for the full native-telephony stack this
# deliberately leaves out — that one requires SIP infra with real routing
# config that lives outside this repo (see docs/setup.md); this one is
# meant to work for anyone who forks the repo and just wants to run and
# talk to an agent.
#
# Prerequisites (see docs/setup.md for install steps):
#   PostgreSQL, Redis, Node.js, and (only if using local models instead of
#   a cloud STT/LLM/TTS provider) Ollama.
#
# Usage: source this file to get helper functions, or copy individual
# blocks into separate terminal tabs. Run start_data, then
# scripts/seed_default_config.py once, then the rest in any order.

set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

# ── Block 1: Data layer (Postgres/Redis; run once, may already be running) ──
start_data() {
  brew services start postgresql@14 2>/dev/null || true
  brew services start redis         2>/dev/null || true
  psql voiceai -f "$REPO/database/schema.sql"
  psql voiceai -f "$REPO/database/knowledge_schema.sql"
  psql voiceai -f "$REPO/database/telephony_schema.sql"
  echo "✓ PostgreSQL + Redis running, schema applied"
  echo "  Next: POSTGRES_DSN=postgresql://\$(whoami)@localhost:5432/voiceai REDIS_URL=redis://localhost:6379/0 $REPO/venv/bin/python3 $REPO/scripts/seed_default_config.py"
}

# ── Block 2: Ollama — only needed if testing with local models (the
#    seed script's default) rather than a cloud STT/LLM/TTS provider ──────
start_ollama() {
  ollama serve
}

# ── Block 3: Config Service (REST API, port 8000) ────────────────────────────
start_config_service() {
  export POSTGRES_DSN="postgresql://$(whoami)@localhost:5432/voiceai"
  cd "$REPO"
  ./venv/bin/python3 -m uvicorn services.config.app:app --host 0.0.0.0 --port 8000
}

# ── Block 4: Knowledge Service (REST API, port 8100) — optional, only
#    needed if testing RAG-backed agents ──────────────────────────────────
start_knowledge_service() {
  export POSTGRES_DSN="postgresql://$(whoami)@localhost:5432/voiceai"
  export REDIS_URL="redis://localhost:6379/0"
  export KNOWLEDGE_STORAGE_ROOT="$REPO/data/knowledge_documents"
  cd "$REPO"
  ./venv/bin/python3 -m uvicorn services.knowledge.app:app --host 0.0.0.0 --port 8100
}

# ── Block 5: ConversationService (gRPC, port 50051) — the actual STT/LLM/
#    TTS pipeline. One instance is enough for testing; run start_conv2 too
#    only if you also want to exercise the Envoy load-balancing path. ─────
_conv_env() {
  export POSTGRES_DSN="postgresql://$(whoami)@localhost:5432/voiceai"
  export REDIS_URL="redis://localhost:6379/0"
  export CONFIG_SERVICE_URL="http://localhost:8000"
  export CONFIG_SERVICE_EMAIL="conversation-service@internal.yuviz.ai"
  export CONFIG_SERVICE_PASSWORD="${CONFIG_SERVICE_PASSWORD:?set this — see docs/setup.md, never commit the real value}"
  export KNOWLEDGE_SERVICE_URL="http://localhost:8100"
}
start_conv1() {
  _conv_env
  cd "$REPO"
  ./venv/bin/python3 -m services.conversation --port 50051 --mode pipeline --log-level INFO
}
start_conv2() {
  _conv_env
  cd "$REPO"
  ./venv/bin/python3 -m services.conversation --port 50052 --mode pipeline --log-level INFO
}

# ── Block 6: webcall bridge (browser WebSocket <-> gRPC, port 8300) ───────
start_webcall() {
  # This script only starts one ConvSvc instance (start_conv1, :50051) and
  # doesn't bring up Envoy, so point directly at it — webcall's own default
  # is Envoy's :10000, which nothing is listening on in this simplified
  # web-test path.
  export CONVERSATION_SVC_TARGET="localhost:50051"
  cd "$REPO"
  ./venv/bin/python3 -m services.webcall --port 8300
}

# ── Block 7: Admin UI (Next.js, port 3000) — has its own "Test Agent"
#    panel (admin-ui/components/TestAgentPanel.tsx) that talks to webcall
#    directly; this is the actual thing you click to test an agent. ──────
start_admin_ui() {
  cd "$REPO/admin-ui"
  npm install
  npm run dev
}

# ── Verify: check the web-testing services are up ─────────────────────────
verify() {
  echo "=== Port check ==="
  for port in 5432 6379 11434 8000 8100 50051 8300 3000; do
    nc -z localhost "$port" 2>/dev/null && echo "  :$port  OPEN" || echo "  :$port  CLOSED"
  done
  echo ""
  echo "=== Config Service health ==="
  curl -s http://localhost:8000/health || echo "  Config Service not reachable"
}

echo "start_web_test.sh loaded. Functions: start_data, start_ollama, start_config_service, start_knowledge_service, start_conv1, start_conv2, start_webcall, start_admin_ui, verify"
echo "Then open http://localhost:3000, go to an agent's page, and click 'Test Agent'."
