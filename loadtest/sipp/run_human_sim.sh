#!/bin/bash
# ─────────────────────────────────────────────────────────────
# Yuviz — SIPp human-caller concurrency load test
#
# Origin: brought in from an external prototype's SIPp harness and
# reconfigured for this repo — same scenario (uac.xml: real RTP audio
# injected via combined_audio.pcap, realistic conversational timing
# with silence gaps for STT+LLM+TTS turnaround), but the LAN IP is now
# auto-detected the same way scripts/update_kamailio_ip.sh does,
# instead of a value hardcoded for one specific machine.
#
# This exists specifically to answer G1 from the scale-gap analysis:
# we have zero real measurement of per-call CPU/memory cost under
# concurrency. A single SIPp run only proves calls complete — it
# doesn't answer G1 unless something is watching resource usage on
# the Gateway/Conversation Service processes while calls are live.
# --monitor does that here.
#
# SIPp's own live-updating screen (call rate/current calls/message
# counters) needs a real terminal — piping its output through `tee` to
# capture a log makes SIPp detect a non-TTY and silently fall back to
# a one-shot final summary, killing the live view. So SIPp runs with
# direct terminal access below (no pipe), and -trace_screen asks SIPp
# itself to periodically snapshot that screen to a file — the live
# view and the persisted log both work, neither steals from the other.
#
# Usage: ./run_human_sim.sh [cps] [concurrent] [total] [--monitor]
# ─────────────────────────────────────────────────────────────
set -euo pipefail

detect_lan_ip() {
  python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('8.8.8.8', 80))
    print(s.getsockname()[0])
finally:
    s.close()
"
}

KAMAILIO_IP="${KAMAILIO_IP:-$(detect_lan_ip)}"
KAMAILIO_PORT="${KAMAILIO_PORT:-5060}"
SERVICE="${SIPP_TEST_DID:-5000}"   # must be in Kamailio's routable 500[0-9] range
RTP_START_PORT="${RTP_START_PORT:-6000}"

RATE=${1:-1}
LIMIT=${2:-3}
TOTAL=${3:-20}
MONITOR=false
for arg in "$@"; do
  [[ "$arg" == "--monitor" ]] && MONITOR=true
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
XML="${SCRIPT_DIR}/uac.xml"
PCAP="${SCRIPT_DIR}/combined_audio.pcap"
RESULTS_DIR="${SCRIPT_DIR}/results/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULTS_DIR"
BANNER_LOG="${RESULTS_DIR}/run_summary.log"

# Prints to the terminal AND appends to run_summary.log — used only for
# this script's own messages (window banner, file checks, monitor
# status). SIPp's own output is never routed through this; see the note
# above about why piping SIPp specifically breaks its live screen.
log() {
  echo "$@" | tee -a "$BANNER_LOG"
}

START_TS=$(date +%s)
START_HUMAN=$(date -r "$START_TS" "+%Y-%m-%d %H:%M:%S %Z")
# Rough expected window: TOTAL calls paced at RATE cps to all get sent,
# plus one full call's scripted duration (60s — matches uac.xml's BYE-wait
# timeout, sized for local-provider round-trip latency) for the last one
# to finish.
EXPECTED_SECS=$(( (TOTAL / RATE) + 60 ))
EXPECTED_END_HUMAN=$(date -r "$((START_TS + EXPECTED_SECS))" "+%H:%M:%S %Z")

log "═══════════════════════════════════════════════════════"
log "  Yuviz SIPp Concurrency Load Test"
log "  Target  : ${KAMAILIO_IP}:${KAMAILIO_PORT} (DID ${SERVICE}) → FS:5080"
log "  Rate    : ${RATE} cps | Concurrent: ${LIMIT} | Total: ${TOTAL}"
log "  RTP port: ${RTP_START_PORT}+"
log "  Monitor : ${MONITOR}"
log "  Results : ${RESULTS_DIR}"
log "  Window  : ${START_HUMAN} → ~${EXPECTED_END_HUMAN} (~${EXPECTED_SECS}s expected)"
log "═══════════════════════════════════════════════════════"

for f in "$XML" "$PCAP"; do
  if [ ! -f "$f" ]; then
    log "[ERROR] Missing file: $f"
    if [[ "$f" == "$PCAP" ]]; then
      log "  combined_audio.pcap is root-owned at the source (/opt/src/testSimulator) —"
      log "  copy it manually: sudo cp /opt/src/testSimulator/combined_audio.pcap '$SCRIPT_DIR/'"
    fi
    exit 1
  fi
done
log "[OK] All files present"
log ""

MONITOR_PID=""
if [[ "$MONITOR" == "true" ]]; then
  MONITOR_LOG="${RESULTS_DIR}/resource_usage.csv"
  echo "timestamp,process,pid,cpu_pct,rss_mb" > "$MONITOR_LOG"
  (
    while true; do
      ts=$(date +%s)
      # gateway, conversation service (both instances), freeswitch, kamailio
      ps -Ao pid,pcpu,rss,command | grep -E "voice_ai_gateway|services\.conversation|freeswitch|kamailio" | grep -v grep | \
        awk -v ts="$ts" '{
          name = "other";
          if ($0 ~ /voice_ai_gateway/) name = "gateway";
          else if ($0 ~ /services\.conversation/) name = "conversation";
          else if ($0 ~ /freeswitch/) name = "freeswitch";
          else if ($0 ~ /kamailio/) name = "kamailio";
          printf "%s,%s,%s,%s,%.1f\n", ts, name, $1, $2, $3/1024
        }' >> "$MONITOR_LOG"
      sleep 2
    done
  ) &
  MONITOR_PID=$!
  log "[OK] Resource monitor started (pid $MONITOR_PID) → $MONITOR_LOG"
fi

cleanup() {
  if [[ -n "$MONITOR_PID" ]]; then
    kill "$MONITOR_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# Note: no -i flag here. Audio injected via play_pcap_audio
# in uac.xml <exec> action (requires PCAP-enabled SIPp build).
# -mp sets base RTP port for media negotiation.
# -trace_screen periodically snapshots SIPp's live screen to
# uac_<pid>_screens.log — this is what replaces piping/tee-ing SIPp's
# own stdout, since that pipe is exactly what kills the live view.
# cd into the results dir first so all of SIPp's own uac_<pid>_*.log
# trace files (it always writes to the CWD, no flag to redirect them)
# land next to stats.csv instead of cluttering the repo.
cd "$RESULTS_DIR"
sipp "${KAMAILIO_IP}:${KAMAILIO_PORT}" \
    -sf  "${XML}"   \
    -s   "${SERVICE}" \
    -r   "${RATE}"    \
    -l   "${LIMIT}"   \
    -m   "${TOTAL}"   \
    -mp  "${RTP_START_PORT}" \
    -trace_msg      \
    -trace_err      \
    -trace_stat     \
    -trace_screen   \
    -stf "${RESULTS_DIR}/stats.csv"

END_TS=$(date +%s)
END_HUMAN=$(date -r "$END_TS" "+%Y-%m-%d %H:%M:%S %Z")
ELAPSED=$((END_TS - START_TS))
ELAPSED_FMT=$(printf '%dm%02ds' $((ELAPSED/60)) $((ELAPSED%60)))

log ""
log "═══════════════════════════════════════════════════════"
log "  Execution window: ${START_HUMAN} → ${END_HUMAN}  (${ELAPSED_FMT} elapsed)"
log "  Results in: ${RESULTS_DIR}"
log "  - stats.csv           call success/failure, response times"
log "  - run_summary.log      this banner + setup messages"
log "  - uac_<pid>_screens.log SIPp's own live-screen snapshots over time"
log "  - uac_<pid>_messages.log full SIP message trace"
[[ "$MONITOR" == "true" ]] && log "  - resource_usage.csv   CPU/RSS sampled every 2s during the run"
log "═══════════════════════════════════════════════════════"
