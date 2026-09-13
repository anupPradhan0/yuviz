#!/usr/bin/env bash
# scripts/setup_telephony_stack.sh
#
# One-shot, idempotent bootstrap for the native macOS telephony stack
# (Kamailio + FreeSWITCH + mod_audio_fork) described end-to-end in
# docs/telephony_stack_setup.md. That doc was written by reverse-engineering
# one working machine by hand; this script turns the "replicate this on your
# laptop" steps from that doc into commands, so a new developer runs one
# script instead of hand-editing 5 XML files and remembering an undocumented
# CONFDIR gotcha.
#
# What this does NOT do (see scripts/start_local.sh instead):
#   - start any long-running service (kamailio/freeswitch/gateway/etc)
#   - realign an already-working stack after a LAN IP change (that's
#     scripts/update_kamailio_ip.sh, which this script also calls once at
#     the very end, since a fresh Kamailio config needs the same rendering)
#
# Safe to re-run: every step checks "is this already done?" before acting.
#
# The one step this script cannot fully guarantee end-to-end is compiling
# FreeSWITCH itself from source — the exact set of system build dependencies
# (autoconf/automake/libtool/pkg-config plus a dozen codec/media libs) varies
# by machine and isn't something this script can silently paper over. If you
# already have a working FreeSWITCH build with mod_audio_fork + mod_lua
# loaded (confirm with `fs_cli -x "module_exists mod_audio_fork"`), set
# SKIP_FS_BUILD=1 and this script will skip straight to the parts that ARE
# fully mechanical: wiring the runtime config files and bootstrapping
# Kamailio.
#
# Usage:
#   ./scripts/setup_telephony_stack.sh
#   SKIP_FS_BUILD=1 ./scripts/setup_telephony_stack.sh
#   SIP_USER=devbox SIP_PASS=changeme ./scripts/setup_telephony_stack.sh

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FS_PREFIX="${FS_PREFIX:-/usr/local/freeswitch}"
FS_SRC="${FS_SRC:-$HOME/src/freeswitch}"
FS_COMMIT="8babcee3eaab4d8b0b60c2a113386419b86bc9d9"
FS_REPO_URL="https://github.com/signalwire/freeswitch.git"
# Prebuilt tarball of this exact commit + the patched mod_audio_fork, arm64
# only — built once (see vendor/mod_audio_fork/PROVENANCE.md) so the other
# two-plus developers on the same architecture don't each recompile
# FreeSWITCH from source. Skipped automatically on non-arm64 machines or if
# FreeSWITCH's own code ever needs a local patch (at which point the
# prebuilt tarball would silently be stale — rebuild and re-publish it, or
# set SKIP_PREBUILT=1 to always build from source).
FS_PREBUILT_URL="${FS_PREBUILT_URL:-https://github.com/yuviz-ai/yuviz/releases/download/freeswitch-prebuilt-8babcee3-arm64/freeswitch-8babcee3-macos-arm64.tar.gz}"
SKIP_PREBUILT="${SKIP_PREBUILT:-}"
SKIP_FS_BUILD="${SKIP_FS_BUILD:-}"
SIP_USER="${SIP_USER:-}"
SIP_PASS="${SIP_PASS:-}"

step() { echo ""; echo "=== $* ==="; }
ok()   { echo "  OK: $*"; }
skip() { echo "  skip: $*"; }
info() { echo "  $*"; }

require_cmd() {
  command -v "$1" > /dev/null 2>&1 || { echo "ERROR: '$1' not found on PATH" >&2; exit 1; }
}

# ---------------------------------------------------------------------------
step "1/5 Homebrew packages"
# ---------------------------------------------------------------------------
require_cmd brew

for pkg in kamailio mysql; do
  if brew list "$pkg" > /dev/null 2>&1; then
    skip "$pkg already installed"
  else
    info "installing $pkg..."
    brew install "$pkg"
    ok "$pkg installed"
  fi
done

# ---------------------------------------------------------------------------
step "2/5 FreeSWITCH + mod_audio_fork (prebuilt download, or source build)"
# ---------------------------------------------------------------------------
fs_already_working() {
  [[ -x "$FS_PREFIX/bin/fs_cli" ]] || return 1
  "$FS_PREFIX/bin/freeswitch" -version > /dev/null 2>&1 || true
  # A build existing on disk is enough to skip — this script only wires
  # source, it doesn't need a *running* process here.
  [[ -x "$FS_PREFIX/bin/freeswitch" ]]
}

need_fs_setup=1
if [[ -n "$SKIP_FS_BUILD" ]]; then
  skip "SKIP_FS_BUILD set — assuming FreeSWITCH + mod_audio_fork already built at $FS_PREFIX"
  need_fs_setup=0
elif fs_already_working && [[ -z "${FORCE_FS_BUILD:-}" ]]; then
  skip "found an existing FreeSWITCH build at $FS_PREFIX — not rebuilding (set FORCE_FS_BUILD=1 to rebuild anyway)"
  need_fs_setup=0
fi

# ---- try the prebuilt tarball first ---------------------------------------
if [[ "$need_fs_setup" == "1" && -z "$SKIP_PREBUILT" ]]; then
  arch="$(uname -m)"
  if [[ "$arch" != "arm64" ]]; then
    info "arch is $arch, not arm64 — the prebuilt tarball only covers Apple Silicon; falling back to source build"
  else
    info "downloading prebuilt FreeSWITCH ($arch, commit ${FS_COMMIT:0:8}) from $FS_PREBUILT_URL ..."
    tmp_tar="$(mktemp -t freeswitch-prebuilt.XXXXXX.tar.gz)"
    if curl -fL --progress-bar -o "$tmp_tar" "$FS_PREBUILT_URL"; then
      if sudo tar xzf "$tmp_tar" -C "$(dirname "$FS_PREFIX")"; then
        rm -f "$tmp_tar"
        ok "extracted prebuilt FreeSWITCH to $FS_PREFIX (only runtime state + config were excluded from the tarball — step 3 below writes fresh config)"
        need_fs_setup=0
      else
        rm -f "$tmp_tar"
        echo "WARNING: failed to extract prebuilt tarball — falling back to source build" >&2
      fi
    else
      rm -f "$tmp_tar"
      echo "WARNING: could not download prebuilt tarball (release asset missing or network issue) — falling back to source build" >&2
    fi
  fi
fi

if [[ "$need_fs_setup" == "1" ]]; then
  if [[ ! -d "$FS_SRC/.git" ]]; then
    info "cloning FreeSWITCH into $FS_SRC (this is a large repo, may take a few minutes)..."
    git clone "$FS_REPO_URL" "$FS_SRC"
  else
    skip "FreeSWITCH source already cloned at $FS_SRC"
  fi

  cd "$FS_SRC"
  current_commit="$(git rev-parse HEAD)"
  if [[ "$current_commit" != "$FS_COMMIT" ]]; then
    info "checking out pinned commit $FS_COMMIT (was $current_commit)..."
    git fetch origin
    git checkout "$FS_COMMIT"
    ok "checked out $FS_COMMIT"
  else
    skip "already at pinned commit $FS_COMMIT"
  fi

  # ---- wire in mod_audio_fork -------------------------------------------
  MOD_DIR="$FS_SRC/src/mod/applications/mod_audio_fork"
  if [[ -f "$MOD_DIR/mod_audio_fork.c" ]] && ! [[ -n "${FORCE_FS_BUILD:-}" ]]; then
    skip "mod_audio_fork source already present in the tree"
  else
    mkdir -p "$MOD_DIR"
    rsync -a --delete "$REPO_ROOT/vendor/mod_audio_fork/src/" "$MOD_DIR/"
    ok "copied vendor/mod_audio_fork/src -> $MOD_DIR (see vendor/mod_audio_fork/PROVENANCE.md for what's patched and why)"
  fi

  # configure.ac needs the module's Makefile listed so autotools generates
  # a Makefile for it — insert right after mod_avmd's entry (its nearest
  # alphabetical neighbour in AC_CONFIG_FILES) if not already present.
  CONFIGURE_AC="$FS_SRC/configure.ac"
  if grep -q "src/mod/applications/mod_audio_fork/Makefile" "$CONFIGURE_AC"; then
    skip "configure.ac already lists mod_audio_fork's Makefile"
  else
    # (indentation of the inserted line is cosmetic only — autotools
    # doesn't care, and BSD sed's \n-in-replacement doesn't repeat a
    # backreference cleanly, so a fixed 4-space indent is used instead)
    sed -i.bak \
      's#src/mod/applications/mod_avmd/Makefile#src/mod/applications/mod_audio_fork/Makefile\n    src/mod/applications/mod_avmd/Makefile#' \
      "$CONFIGURE_AC"
    if grep -q "src/mod/applications/mod_audio_fork/Makefile" "$CONFIGURE_AC"; then
      rm -f "$CONFIGURE_AC.bak"
      ok "added mod_audio_fork's Makefile to configure.ac"
    else
      mv "$CONFIGURE_AC.bak" "$CONFIGURE_AC"
      echo "WARNING: could not auto-insert into configure.ac (mod_avmd anchor line not found)." >&2
      echo "  Add this line yourself inside AC_CONFIG_FILES([...]) in $CONFIGURE_AC:" >&2
      echo "    src/mod/applications/mod_audio_fork/Makefile" >&2
    fi
  fi

  # modules.conf is the plain-text "which optional modules to build" list —
  # ensure an uncommented applications/mod_audio_fork line exists.
  MODULES_CONF="$FS_SRC/modules.conf"
  if [[ -f "$MODULES_CONF" ]] && grep -qx "applications/mod_audio_fork" "$MODULES_CONF"; then
    skip "modules.conf already enables mod_audio_fork"
  elif [[ -f "$MODULES_CONF" ]] && grep -q "^#applications/mod_audio_fork$" "$MODULES_CONF"; then
    sed -i.bak 's/^#applications\/mod_audio_fork$/applications\/mod_audio_fork/' "$MODULES_CONF"
    rm -f "$MODULES_CONF.bak"
    ok "uncommented applications/mod_audio_fork in modules.conf"
  else
    echo "applications/mod_audio_fork" >> "$MODULES_CONF"
    ok "appended applications/mod_audio_fork to modules.conf"
  fi
  if [[ -f "$MODULES_CONF" ]] && ! grep -qx "languages/mod_lua" "$MODULES_CONF"; then
    if grep -q "^#languages/mod_lua$" "$MODULES_CONF"; then
      sed -i.bak 's/^#languages\/mod_lua$/languages\/mod_lua/' "$MODULES_CONF"
      rm -f "$MODULES_CONF.bak"
    else
      echo "languages/mod_lua" >> "$MODULES_CONF"
    fi
    ok "enabled languages/mod_lua in modules.conf"
  else
    skip "modules.conf already enables mod_lua"
  fi

  echo ""
  echo "  Ready to build. This step is the one part of this script that varies"
  echo "  most by machine (build deps for FreeSWITCH's own codecs/media libs)"
  echo "  and can take 20-60+ minutes. Run it yourself so failures are visible:"
  echo ""
  echo "    cd $FS_SRC"
  echo "    ./bootstrap.sh -j"
  echo "    ./configure --prefix=$FS_PREFIX"
  echo "    make -j\$(sysctl -n hw.ncpu)"
  echo "    sudo make install"
  echo ""
  echo "  Then confirm both modules loaded:"
  echo "    $FS_PREFIX/bin/fs_cli -x 'module_exists mod_audio_fork'   # must print true"
  echo "    $FS_PREFIX/bin/fs_cli -x 'module_exists mod_lua'          # must print true"
  echo ""
  echo "  Re-run this script afterwards (or with SKIP_FS_BUILD=1) to continue"
  echo "  with the runtime config + Kamailio steps below."
  echo ""
  echo "  (You ended up here because the prebuilt tarball wasn't used — either"
  echo "  this isn't arm64, SKIP_PREBUILT was set, or the download/extract"
  echo "  failed above. If FreeSWITCH itself needed no local changes, building"
  echo "  once and sharing the result beats every developer compiling it — see"
  echo "  \$FS_PREBUILT_URL at the top of this script.)"
  cd "$REPO_ROOT"
fi

# ---------------------------------------------------------------------------
step "3/5 FreeSWITCH runtime config (dialplan, Lua bridge, SIP profile, ESL, modules)"
# ---------------------------------------------------------------------------
if [[ ! -x "$FS_PREFIX/bin/fsxs" ]]; then
  echo "  $FS_PREFIX/bin/fsxs not found — FreeSWITCH isn't built/installed yet."
  echo "  Skipping runtime config; re-run this script once the build above completes."
else
  # The documented gotcha (docs/telephony_stack_setup.md §3): don't assume
  # $FS_PREFIX/conf/ is live — ask the binary itself which CONFDIR it was
  # compiled with.
  CONFDIR="$(grep CONFDIR "$FS_PREFIX/bin/fsxs" | head -1 | sed -E "s/.*CONFDIR *=> *'([^']+)'.*/\1/")"
  if [[ -z "$CONFDIR" || ! -d "$CONFDIR" ]]; then
    echo "  ERROR: could not determine FreeSWITCH's real CONFDIR from fsxs" >&2
    exit 1
  fi
  ok "real CONFDIR is $CONFDIR"

  # 3a. dialplan extension
  DIALPLAN_FILE="$CONFDIR/dialplan/public/voice_ai.xml"
  mkdir -p "$(dirname "$DIALPLAN_FILE")"
  cat > "$DIALPLAN_FILE" <<'XML'
<include>
  <!--
    Voice AI extension — generic, permanent entry point for ALL AI-routed
    calls. This file is never edited again when a tenant buys a new DID,
    reassigns a number to a different agent, or changes providers — all of
    that is a phone_numbers/agents/provider_configs row change via the
    Config Service REST API, nothing here.

    Matches any realistic DID (4-15 digits, optional leading +) — covers
    both short local test extensions (5000-5002) and real E.164 numbers
    (e.g. +14085551000) with the same single rule. Deliberately NOT a
    mod_xml_curl dynamic dialplan (considered and rejected for this use
    case): the DID itself carries no routing information FreeSWITCH needs
    to act on, it's just forwarded as-is to the Gateway, which does 100%
    of the tenant/agent resolution via Redis
    (PhoneRoute::from_redis("did:" + destination_number)). FreeSWITCH's
    job here is strictly telephony (SIP + media), never business routing.

    Prerequisites: gateway running on :8080, Conversation Service on :50051
    (or Envoy :10000).
    Audio path: SIP softphone -> FreeSWITCH -> mod_audio_fork -> gateway WS -> Python AI service
    TTS return:  Python AI -> gateway WS -> mod_audio_fork -> FreeSWITCH -> softphone speaker
  -->
  <extension name="voice-ai">
    <condition field="destination_number" expression="^\+?[0-9]{4,15}$">
      <!-- Answer immediately; 200ms pause lets SIP RTP path stabilise -->
      <action application="answer"/>
      <action application="sleep" data="200"/>

      <action application="log" data="NOTICE === Attaching mod_audio_fork to Voice AI uuid=${uuid} did=${destination_number} ani=${caller_id_number} ==="/>
      <!-- Auto-start audio streaming to Voice AI via Lua -->
      <action application="set" data="send_silence_when_idle=400"/>
      <action application="lua" data="start_voice_ai.lua ${uuid}"/>
      <action application="park"/>
    </condition>
  </extension>
</include>
XML
  ok "wrote $DIALPLAN_FILE"

  # 3b. Lua bridge script — mod_lua's script search path resolves to
  # share/freeswitch/scripts, NOT etc/freeswitch/scripts (confirmed on the
  # reference machine); derive it from CONFDIR's known sibling layout.
  FS_SHARE_SCRIPTS="$FS_PREFIX/share/freeswitch/scripts"
  mkdir -p "$FS_SHARE_SCRIPTS"
  LUA_FILE="$FS_SHARE_SCRIPTS/start_voice_ai.lua"
  cat > "$LUA_FILE" <<'LUA'
local uuid      = argv[1]
local did       = session:getVariable("destination_number") or ""
local ani       = session:getVariable("caller_id_number") or ""
local direction = "inbound"

api = freeswitch.API()
local metadata = string.format(
    '{"type":"start","call_id":"%s","did":"%s","ani":"%s","direction":"%s"}',
    uuid, did, ani, direction)
local ws_url = "ws://127.0.0.1:8080/voice/" .. uuid
local result = api:execute("uuid_audio_fork", uuid .. " start " .. ws_url .. " mono 16000 " .. metadata)
freeswitch.consoleLog("NOTICE", "=== LUA STT started uuid=" .. uuid .. " did=" .. did .. " ani=" .. ani .. " result=" .. tostring(result) .. "\n")
LUA
  chmod +x "$LUA_FILE"
  ok "wrote $LUA_FILE"

  # 3c/3d/3e. XML param patches — small, targeted, idempotent regex edits
  # rather than a full XML rewrite, so untouched parts of each file (which
  # vary machine to machine) are left alone.
  python3 - "$CONFDIR" <<'PYEOF'
import re
import sys

confdir = sys.argv[1]

def patch(path, transform, label):
    with open(path) as f:
        original = f.read()
    updated = transform(original)
    if updated == original:
        print(f"  skip: {label} already correct")
    else:
        with open(path, "w") as f:
            f.write(updated)
        print(f"  OK: {label}")

def set_param(xml, name, value):
    pattern = re.compile(rf'<param\s+name="{name}"\s+value="[^"]*"\s*/>')
    replacement = f'<param name="{name}" value="{value}"/>'
    if pattern.search(xml):
        return pattern.sub(replacement, xml)
    # commented-out form: uncomment and set value
    commented = re.compile(rf'<!--\s*(<param\s+name="{name}"\s+value="[^"]*"\s*/>)\s*-->')
    if commented.search(xml):
        return commented.sub(replacement, xml)
    return xml

sip_profile = f"{confdir}/sip_profiles/external.xml"
patch(sip_profile,
      lambda xml: set_param(set_param(xml, "dialplan", "XML"), "context", "public"),
      "sip_profiles/external.xml dialplan/context")

esl_conf = f"{confdir}/autoload_configs/event_socket.conf.xml"
patch(esl_conf,
      lambda xml: set_param(set_param(xml, "listen-port", "8022"), "password", "ClueCon"),
      "event_socket.conf.xml listen-port/password")

modules_conf = f"{confdir}/autoload_configs/modules.conf.xml"
def ensure_loaded(xml, module):
    if re.search(rf'<load\s+module="{module}"\s*/>', xml):
        return xml
    return xml.replace("</modules>", f'  <load module="{module}"/>\n  </modules>')
patch(modules_conf,
      lambda xml: ensure_loaded(ensure_loaded(xml, "mod_audio_fork"), "mod_lua"),
      "modules.conf.xml mod_audio_fork/mod_lua")
PYEOF
fi

# ---------------------------------------------------------------------------
step "4/5 Kamailio bootstrap (MySQL schema, subscriber, config templates)"
# ---------------------------------------------------------------------------
require_cmd kamdbctl
require_cmd kamctl

if mysql -u root -e "USE kamailio;" > /dev/null 2>&1; then
  skip "kamailio MySQL database already exists"
else
  info "running kamdbctl create (follow its prompts)..."
  kamdbctl create
  ok "kamailio database created"
fi

if [[ -n "$SIP_USER" && -n "$SIP_PASS" ]]; then
  if kamctl db show subscriber 2>/dev/null | grep -q "^$SIP_USER "; then
    skip "subscriber '$SIP_USER' already exists"
  else
    kamctl add "$SIP_USER" "$SIP_PASS"
    ok "added subscriber '$SIP_USER'"
  fi
else
  info "SIP_USER/SIP_PASS not set — skipping subscriber creation."
  info "Add one yourself (match whatever scripts/kamailio/kamailio.cfg.tpl expects FreeSWITCH to register as):"
  info "  kamctl add <username> <password>"
fi

info "rendering Kamailio config from templates for this machine's LAN IP..."
"$REPO_ROOT/scripts/update_kamailio_ip.sh"

# ---------------------------------------------------------------------------
step "5/5 Done"
# ---------------------------------------------------------------------------
cat <<EOF

Next: start everything (see docs/telephony_stack_setup.md §6-7 for detail):

  source scripts/start_local.sh
  start_mysql
  start_kamailio
  start_data
  start_freeswitch
  # build the Gateway once: cmake -B build/gateway gateway && cmake --build build/gateway
  start_gateway
  start_envoy
  start_conv1
  verify

Then register a SIP softphone against Kamailio with the subscriber
credentials above and place a test call.
EOF
