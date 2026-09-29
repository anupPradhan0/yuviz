#!/usr/bin/env bash
# setup_macos.sh — Native (no VM) FreeSWITCH for the local softphone path.
#
#   Zoiper → Kamailio :5060 → FreeSWITCH :5080 → mod_audio_fork → Gateway :8080
#
# Uses Homebrew's bottled arm64 FreeSWITCH, builds mod_audio_fork against its
# headers, and patches the Homebrew config in place. Idempotent: safe to rerun
# (e.g. after `brew upgrade freeswitch`, which drops the built module).
#
# Usage: scripts/freeswitch/setup_macos.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$REPO_ROOT/scripts/freeswitch"
SRC_DIR="${AUDIO_FORK_SRC:-$HOME/src/drachtio-freeswitch-modules}"
# The canonical drachtio repo was taken down; this is a maintained mirror.
SRC_REPO="https://github.com/mdslaney/drachtio-freeswitch-modules"

brew list freeswitch >/dev/null 2>&1 || brew install freeswitch
brew list libwebsockets >/dev/null 2>&1 || brew install libwebsockets

FS_PREFIX="$(brew --prefix freeswitch)"
MOD_DIR="$FS_PREFIX/lib/freeswitch/mod"
# Our own copy of the config: Homebrew's etc/freeswitch is symlinks into the
# versioned Cellar, so editing it in place would be lost on `brew upgrade`.
# start_freeswitch (scripts/start_local.sh) runs FreeSWITCH with -conf here.
FS_HOME="${FS_HOME:-$HOME/.yuviz/freeswitch}"
CONF="$FS_HOME/conf"

# ── Step 1: build mod_audio_fork ────────────────────────────────────────────
echo "=== Step 1/3: mod_audio_fork ==="
[[ -d "$SRC_DIR" ]] || git clone --depth 1 "$SRC_REPO" "$SRC_DIR"
# Upstream discards binary frames from the far end, but the Gateway sends the
# agent's speech back as binary L16 — without this patch the caller hears
# nothing. The patch plays those frames into the call via a write-replace bug.
if git -C "$SRC_DIR" apply --check "$HERE/mod_audio_fork-playback.patch" 2>/dev/null; then
  git -C "$SRC_DIR" apply "$HERE/mod_audio_fork-playback.patch"
  echo "  applied mod_audio_fork-playback.patch"
elif ! git -C "$SRC_DIR" apply --reverse --check "$HERE/mod_audio_fork-playback.patch" 2>/dev/null; then
  echo "ERROR: mod_audio_fork-playback.patch does not apply to $SRC_DIR" >&2
  exit 1
fi
(
  cd "$SRC_DIR/modules/mod_audio_fork"
  export PKG_CONFIG_PATH="$FS_PREFIX/lib/pkgconfig:$(brew --prefix libwebsockets)/lib/pkgconfig:$(brew --prefix speexdsp)/lib/pkgconfig:$(brew --prefix openssl@3)/lib/pkgconfig"
  # _DARWIN_C_SOURCE: FreeSWITCH's cflags put the SDK in strict POSIX mode,
  # which hides the BSD u_char/u_short that <net/ethernet.h> needs.
  CFLAGS="$(pkg-config --cflags freeswitch libwebsockets speexdsp) -I$(brew --prefix openssl@3)/include -D_DARWIN_C_SOURCE -fPIC -O2 -Wno-deprecated-declarations"
  LIBS="$(pkg-config --libs freeswitch libwebsockets speexdsp)"
  clang -c $CFLAGS mod_audio_fork.c -o mod_audio_fork.o
  for f in lws_glue parser audio_pipe; do clang++ -std=c++14 -c $CFLAGS "$f.cpp" -o "$f.o"; done
  clang++ -shared -undefined dynamic_lookup -o mod_audio_fork.so ./*.o $LIBS
  cp mod_audio_fork.so "$MOD_DIR/"
)
echo "  installed $MOD_DIR/mod_audio_fork.so"
echo ""

# ── Step 2: patch the Homebrew config ───────────────────────────────────────
echo "=== Step 2/3: FreeSWITCH config ($CONF) ==="
if [[ ! -d "$CONF" ]]; then
  mkdir -p "$FS_HOME"/{log,db,run}
  cp -RL "$(brew --prefix)/etc/freeswitch" "$CONF"
  echo "  copied stock config from Homebrew"
fi

modules="$CONF/autoload_configs/modules.conf.xml"
if ! grep -q 'mod_audio_fork' "$modules"; then
  sed -i '' 's|\(.*<load module="mod_lua"/>.*\)|\1\n    <load module="mod_audio_fork"/>|' "$modules"
  echo "  modules.conf: + mod_audio_fork"
fi

# ESL on loopback only — the Gateway and campaigns service are on this host,
# and ESL is remote control of the whole switch. Port 8022 is pinned by
# config/gateway.yaml and services/campaigns/originate.py.
esl="$CONF/autoload_configs/event_socket.conf.xml"
sed -i '' -e 's|name="listen-ip" value="[^"]*"|name="listen-ip" value="127.0.0.1"|' \
          -e 's|name="listen-port" value="[^"]*"|name="listen-port" value="8022"|' "$esl"
echo "  event_socket: 127.0.0.1:8022"

# The stock `internal` profile binds :5060, which is Kamailio's port on this
# same host. Only `external` (:5080) is used — Kamailio dispatches to it.
for p in internal.xml internal-ipv6.xml external-ipv6.xml; do
  if [[ -f "$CONF/sip_profiles/$p" ]]; then
    mv "$CONF/sip_profiles/$p" "$CONF/sip_profiles/$p.disabled"
    echo "  sip_profiles: disabled $p"
  fi
done

# Default is a STUN lookup, which advertises the public IP in SDP — the
# softphone then sends RTP to the router and the call is silent.
vars="$CONF/vars.xml"
sed -i '' -e 's|data="external_rtp_ip=stun:[^"]*"|data="external_rtp_ip=$${local_ip_v4}"|' \
          -e 's|data="external_sip_ip=stun:[^"]*"|data="external_sip_ip=$${local_ip_v4}"|' "$vars"
echo "  vars: external_{rtp,sip}_ip = local_ip_v4"

# SIP_IP pins local_ip_v4 (FreeSWITCH otherwise auto-detects the
# default-route interface, which may be a VPN). Must match the IP Kamailio
# listens on — see scripts/update_kamailio_ip.sh.
if [[ -n "${SIP_IP:-}" ]]; then
  sed -i '' '/data="local_ip_v4=/d' "$vars"
  sed -i '' "s|^<include>|<include>\n  <X-PRE-PROCESS cmd=\"set\" data=\"local_ip_v4=$SIP_IP\"/>|" "$vars"
  echo "  vars: local_ip_v4 = $SIP_IP"
fi

cp "$HERE/00_voice_ai.xml" "$CONF/dialplan/public/00_voice_ai.xml"
echo "  dialplan: public/00_voice_ai.xml (788, 5000-5009 -> start_voice_ai.lua)"
echo ""

# ── Step 3: sanity check ────────────────────────────────────────────────────
echo "=== Step 3/3: check ==="
echo "  Start with:  start_freeswitch   (from scripts/start_local.sh)"
echo "  Then verify: fs_cli -P 8022 -x 'module_exists mod_audio_fork'"
echo ""
echo "Done."
