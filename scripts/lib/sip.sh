# SIP-side helpers shared by start_local.sh and update_kamailio_ip.sh.
# Source after setting REPO. Every path is user-owned: nothing here runs as root.

KAMAILIO_DIR="${KAMAILIO_DIR:-$HOME/.yuviz/kamailio}"
YUVIZ_LOGS="${YUVIZ_LOGS:-$HOME/.yuviz/logs}"
# Where Kamailio's config lived before it moved to KAMAILIO_DIR; a process
# still running from it is restarted onto the new one.
KAMAILIO_LEGACY_CFG="${KAMAILIO_LEGACY_CFG-/usr/local/etc/kamailio/kamailio.cfg}"
NETWORK_SYNC_LABEL=ai.yuviz.network-sync

# The address the default route leaves from. A UDP connect sends no packet.
_detect_lan_ip() {
  python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('8.8.8.8', 80))
    print(s.getsockname()[0])
except OSError:
    pass
finally:
    s.close()
"
}

# SIP_IP: 127.0.0.1 (default; this Mac only, never changes), auto (follow the
# LAN IP, so phones on the same Wi-Fi can register), or a fixed address.
_sip_target_ip() {
  local mode="${SIP_IP:-127.0.0.1}"
  if [ "$mode" = auto ]; then _detect_lan_ip; else echo "$mode"; fi
}

# FS_PREFIX, when set, is the only candidate; otherwise Homebrew, the source
# install, then Debian's /usr.
_fs_prefix() {
  if [ -n "${FS_PREFIX:-}" ]; then
    [ -x "$FS_PREFIX/bin/freeswitch" ] && echo "$FS_PREFIX"
    return
  fi
  local p
  for p in "$(brew --prefix freeswitch 2>/dev/null)" /usr/local/freeswitch /usr; do
    [ -n "$p" ] && [ -x "$p/bin/freeswitch" ] && { echo "$p"; return 0; }
  done
  return 1
}

_fs_home() { echo "${FS_HOME:-$HOME/.yuviz/freeswitch}"; }

# The vars.xml of the install _fs_start runs.
_fs_vars_xml() {
  local prefix f
  prefix=$(_fs_prefix) || return 1
  for f in "$(_fs_home)/conf/vars.xml" "$prefix/etc/freeswitch/vars.xml" /etc/freeswitch/vars.xml; do
    [ -f "$f" ] && { echo "$f"; return 0; }
  done
  return 1
}

# Runs FreeSWITCH in the foreground; pass -nc to daemonize.
_fs_start() {
  local prefix fs_home
  prefix=$(_fs_prefix) || { echo "FreeSWITCH not found — run scripts/freeswitch/setup_macos.sh" >&2; return 1; }
  fs_home=$(_fs_home)
  if [ -d "$fs_home/conf" ]; then
    # -scripts points at the repo so start_voice_ai.lua is never a stale copy.
    "$prefix/bin/freeswitch" "$@" -nonat -conf "$fs_home/conf" -log "$fs_home/log" \
      -db "$fs_home/db" -run "$fs_home/run" -scripts "$REPO/scripts/freeswitch"
  else
    "$prefix/bin/freeswitch" "$@" -nonat
  fi
}

# Runs Kamailio in the foreground from the rendered config.
_kamailio_start() {
  mkdir -p "$KAMAILIO_DIR/run"
  kamailio -f "$KAMAILIO_DIR/kamailio.cfg" -D -E -Y "$KAMAILIO_DIR/run"
}

_kamailio_pids() {
  pgrep -u "$(id -u)" -f "^([^ ]*/)?kamailio -f $KAMAILIO_DIR/kamailio.cfg"
  [ -n "$KAMAILIO_LEGACY_CFG" ] && pgrep -u "$(id -u)" -f "^([^ ]*/)?kamailio -f $KAMAILIO_LEGACY_CFG"
  return 0
}

# launchd agent that reruns update_kamailio_ip.sh whenever the network changes.
_network_sync_plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$NETWORK_SYNC_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$REPO/scripts/update_kamailio_ip.sh</string>
    <string>--if-changed</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$REPO/venv/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>WatchPaths</key>
  <array>
    <string>/var/run/resolv.conf</string>
    <string>/Library/Preferences/SystemConfiguration</string>
  </array>
  <!-- Unset, launchd throttles the job's CPU, and the services it restarts inherit it. -->
  <key>ProcessType</key><string>Interactive</string>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>AbandonProcessGroup</key><true/>
  <key>StandardOutPath</key><string>$YUVIZ_LOGS/network-sync.log</string>
  <key>StandardErrorPath</key><string>$YUVIZ_LOGS/network-sync.log</string>
</dict>
</plist>
EOF
}
