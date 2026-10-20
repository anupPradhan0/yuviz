#!/usr/bin/env bash
# Opt-in: run supervisord as a macOS LaunchAgent (starts at login, restarted if it dies).
# Usage: deployment/autostart.sh install | uninstall | status | print  [--force: cut live calls off]
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="ai.yuviz.supervisord"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CONF="$REPO/deployment/supervisord.conf"
SOCK="$HOME/.yuviz/supervisor.sock"

[ "$(uname)" = Darwin ] || { echo "autostart is macOS only" >&2; exit 1; }

plist() {
  local supervisord
  supervisord="$(command -v supervisord)" || { echo "supervisord not found: brew install supervisor" >&2; exit 1; }
  # launchd jobs don't inherit the shell PATH; capture it (Homebrew, func-e, the venv) at install time.
  local path="$REPO/venv/bin:$PATH"
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$supervisord</string><string>-c</string><string>$CONF</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$path</string><key>HOME</key><string>$HOME</string></dict>
  <key>RunAtLoad</key><true/>
  <!-- Restarted if it dies, but a clean shutdown (deployment/svc shutdown) stays down. -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <!-- Longer than every stopwaitsecs added up, so launchd never SIGKILLs supervisord mid-stop and orphans services. -->
  <key>ExitTimeOut</key><integer>360</integer>
  <!-- An untyped job is throttled by macOS, which slows call audio. -->
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME/.yuviz/logs/supervisord.launchd.log</string>
  <key>StandardErrorPath</key><string>$HOME/.yuviz/logs/supervisord.launchd.log</string>
</dict>
</plist>
EOF
}

loaded() { launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; }

running_elsewhere() {
  local pid
  pid=$(cat "$HOME/.yuviz/supervisord.pid" 2>/dev/null) && kill -0 "$pid" 2>/dev/null && ! loaded
}

# Shuts the loaded agent's supervisord down through deployment/svc, which refuses while calls are live.
stop_loaded() {
  loaded || return 0
  if [ -S "$SOCK" ]; then
    "$REPO/deployment/svc" shutdown ${FORCE:+--force} || { echo "not changing the LaunchAgent while calls are live" >&2; exit 1; }
  fi
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  local _
  for _ in $(seq 1 60); do loaded || return 0; sleep 0.5; done
  echo "the old LaunchAgent did not unload within 30 s" >&2
  exit 1
}

FORCE=""
for a in "$@"; do [ "$a" = --force ] && FORCE=1; done

case "${1:-status}" in
  print)
    plist
    ;;
  install)
    if running_elsewhere; then
      echo "supervisord is already running (probably in a terminal tab). Stop it with Ctrl-C there, then re-run install." >&2
      exit 1
    fi
    mkdir -p "$HOME/.yuviz/logs" "$(dirname "$PLIST")"
    plist > "$PLIST"
    plutil -lint "$PLIST" >/dev/null
    stop_loaded
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "installed $PLIST; supervisord now starts at login. Check: deployment/svc status"
    ;;
  uninstall)
    # Stops supervisord and every service it runs.
    stop_loaded
    rm -f "$PLIST"
    echo "removed $LABEL"
    ;;
  status)
    if loaded; then
      launchctl print "gui/$(id -u)/$LABEL" | grep -E "^\s+(state|pid|last exit code) =" || true
    else
      echo "not installed"
    fi
    ;;
  *)
    echo "usage: $0 install | uninstall | status | print" >&2
    exit 2
    ;;
esac
