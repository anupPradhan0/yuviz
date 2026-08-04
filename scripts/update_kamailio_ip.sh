#!/usr/bin/env bash
# Realigns the whole local stack with the machine's current LAN IP after a
# network change (new Wi-Fi, switching to a phone hotspot, etc).
#
# Three independent things go stale when the network changes, and all three
# have to be fixed together or calls silently break in different ways:
#
#   1. Kamailio's kamailio.cfg/dispatcher.list — the listen address and the
#      FreeSWITCH routing target are hardcoded (7 occurrences across the two
#      files), regenerated here from scripts/kamailio/*.tpl.
#   2. The kamailio MySQL `subscriber` table — auth_db matches REGISTER/INVITE
#      requests by (username, domain), and ha1/ha1b are MD5 digests that bake
#      the domain in. A stale domain here causes 403s that look like generic
#      "call rejected" failures even after (1) is fixed.
#   3. FreeSWITCH's local_ip_v4 — this is a core-level variable resolved once
#      at process startup (not on every "reloadxml"), so a FreeSWITCH process
#      that's been running since before the network change stays bound to the
#      old IP until it's restarted.
#
# Everything else in the stack (Gateway, Envoy, Postgres/Redis DSNs, admin-ui,
# the FreeSWITCH Lua dialplan script) is already 127.0.0.1/localhost/0.0.0.0
# and needs no changes.
#
# Idempotent: safe to run any time, even if the IP hasn't changed. Each of the
# three steps independently detects "already correct" and skips.
#
# Privilege model: run this as your normal user, NOT via sudo. The two
# operations that need root (writing into /usr/local/etc/kamailio, and
# restarting the kamailio process, which binds a privileged port) escalate
# individually via `sudo` — you'll get one password prompt, cached for the
# rest of the run. Everything else (MySQL, FreeSWITCH) runs as you, which
# matters: FreeSWITCH must keep running as your user, not root, or its
# runtime files end up with the wrong ownership.
#
# Usage: scripts/update_kamailio_ip.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TPL_DIR="$REPO_ROOT/scripts/kamailio"
KAMAILIO_ETC="/usr/local/etc/kamailio"
KAMAILIO_CFG="$KAMAILIO_ETC/kamailio.cfg"
FS_CLI="/usr/local/freeswitch/bin/fs_cli"
FS_BIN="/usr/local/freeswitch/bin/freeswitch"
FS_ESL_PORT=8022
FS_ESL_PASSWORD=ClueCon

# UDP "connect" doesn't send a packet (no handshake) — the OS just resolves
# which local interface/IP would be used to reach that destination, which is
# exactly the current LAN IP regardless of interface name (en0/en1/etc, which
# changes across machines and Wi-Fi vs Ethernet). Works even without real
# connectivity to 8.8.8.8, since nothing is actually transmitted for a UDP
# socket's connect().
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

fs_cli() {
  "$FS_CLI" -H 127.0.0.1 -P "$FS_ESL_PORT" -p "$FS_ESL_PASSWORD" -x "$1" 2>/dev/null
}

LAN_IP="$(detect_lan_ip)"
if [[ -z "$LAN_IP" || "$LAN_IP" == "127.0.0.1" ]]; then
  echo "ERROR: could not detect a real LAN IP (got '$LAN_IP') — is Wi-Fi/Ethernet connected?" >&2
  exit 1
fi
echo "Detected LAN IP: $LAN_IP"
echo ""

# ── Step 1: regenerate Kamailio config from templates ───────────────────────
echo "=== Step 1/3: Kamailio config ==="

kamailio_changed=0
for name in kamailio.cfg dispatcher.list; do
  tpl="$TPL_DIR/$name.tpl"
  target="$KAMAILIO_ETC/$name"

  if [[ ! -f "$tpl" ]]; then
    echo "ERROR: template not found: $tpl" >&2
    exit 1
  fi

  # Generate into a real temp file, not a shell variable — command
  # substitution silently strips trailing newlines, which would make an
  # unchanged file look "different" from the on-disk original on every run.
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' EXIT
  sed "s/__LAN_IP__/$LAN_IP/g" "$tpl" > "$tmp"

  if sudo diff -q "$tmp" "$target" > /dev/null 2>&1; then
    echo "  $name: already up to date (IP unchanged)"
    rm -f "$tmp"
    trap - EXIT
    continue
  fi

  # Back up whatever's currently deployed before overwriting — timestamped,
  # so this never collides with or clobbers any backup you've made by hand
  # (kamailio.cfg_orig, kamailio.cfg_working_push_notification, etc. are
  # left untouched).
  if sudo test -f "$target"; then
    backup="$target.bak.$(date +%Y%m%d%H%M%S)"
    sudo cp "$target" "$backup"
    echo "  $name: backed up existing file to $(basename "$backup")"
  fi

  sudo mv "$tmp" "$target"
  trap - EXIT
  echo "  $name: regenerated with IP=$LAN_IP"
  kamailio_changed=1
done

if [[ "$kamailio_changed" == "1" ]] && pgrep -x kamailio > /dev/null 2>&1; then
  echo "  restarting kamailio to pick up the new config..."
  sudo pkill -x kamailio || true
  for _ in $(seq 1 20); do
    pgrep -x kamailio > /dev/null 2>&1 || break
    sleep 0.5
  done
  sudo kamailio -DD -E -f "$KAMAILIO_CFG"
  sleep 1
  if pgrep -x kamailio > /dev/null 2>&1; then
    echo "  kamailio restarted"
  else
    echo "  WARNING: kamailio did not come back up — check its config with:" >&2
    echo "    sudo kamailio -c -f $KAMAILIO_CFG" >&2
  fi
elif [[ "$kamailio_changed" == "1" ]]; then
  echo "  kamailio isn't currently running — nothing to restart"
fi
echo ""

# ── Step 2: fix up the MySQL subscriber table's auth domain ─────────────────
echo "=== Step 2/3: MySQL subscriber auth domain ==="

if ! command -v mysql > /dev/null 2>&1; then
  echo "  mysql client not found on PATH — skipping"
elif ! mysql -u root kamailio -e "SELECT 1" > /dev/null 2>&1; then
  echo "  can't reach the kamailio MySQL database — skipping"
else
  stale_rows="$(mysql -u root kamailio -N -e \
    "SELECT username, password FROM subscriber WHERE domain != '$LAN_IP';")"

  if [[ -z "$stale_rows" ]]; then
    echo "  all subscriber rows already use domain=$LAN_IP"
  else
    sql_file="$(mktemp)"
    trap 'rm -f "$sql_file"' EXIT
    # ha1/ha1b bake the domain into an MD5 digest, so the domain column and
    # the hashes have to be updated together or auth_db's digest check fails
    # even though the row now "looks" right. Recomputed here in Python rather
    # than MySQL's MD5() because MySQL 9.x removed the builtin in favor of an
    # optional component that isn't installed on this box.
    python3 -c "
import sys

new_domain = '$LAN_IP'
rows = '''$stale_rows'''.strip().splitlines()
for row in rows:
    username, password = row.split('\t')
    import hashlib
    ha1 = hashlib.md5(f'{username}:{new_domain}:{password}'.encode()).hexdigest()
    ha1b = hashlib.md5(f'{username}@{new_domain}:{new_domain}:{password}'.encode()).hexdigest()
    print(f\"UPDATE subscriber SET domain='{new_domain}', ha1='{ha1}', ha1b='{ha1b}' WHERE username='{username}';\")
" > "$sql_file"
    mysql -u root kamailio < "$sql_file"
    rm -f "$sql_file"
    trap - EXIT
    n="$(echo "$stale_rows" | wc -l | tr -d ' ')"
    echo "  updated $n subscriber row(s) to domain=$LAN_IP"
  fi
fi
echo ""

# ── Step 3: restart FreeSWITCH if it's still bound to a stale IP ────────────
echo "=== Step 3/3: FreeSWITCH local_ip_v4 ==="

if [[ ! -x "$FS_CLI" ]]; then
  echo "  fs_cli not found — skipping"
elif ! fs_running_ip="$(fs_cli 'eval ${local_ip_v4}')" || [[ -z "$fs_running_ip" ]]; then
  echo "  FreeSWITCH isn't running (or ESL isn't reachable) — nothing to restart"
elif [[ "$fs_running_ip" == "$LAN_IP" ]]; then
  echo "  FreeSWITCH already bound to $LAN_IP"
else
  echo "  FreeSWITCH is still bound to stale IP $fs_running_ip — restarting"
  # local_ip_v4 is resolved once at process startup, not on "reloadxml" or a
  # sofia profile restart — a full process restart is the only way to pick
  # up a network change that happened while FreeSWITCH kept running.
  fs_cli shutdown || true
  for _ in $(seq 1 20); do
    pgrep -x freeswitch > /dev/null 2>&1 || break
    sleep 0.5
  done
  ( cd /usr/local/freeswitch && "$FS_BIN" -nc )
  sleep 2
  new_ip="$(fs_cli 'eval ${local_ip_v4}' || true)"
  if [[ "$new_ip" == "$LAN_IP" ]]; then
    echo "  FreeSWITCH restarted, now bound to $LAN_IP"
  else
    echo "  WARNING: FreeSWITCH restarted but reports local_ip_v4=$new_ip (expected $LAN_IP)" >&2
  fi
fi

echo ""
echo "Done."
