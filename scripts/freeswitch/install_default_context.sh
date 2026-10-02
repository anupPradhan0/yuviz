#!/usr/bin/env bash
# install_default_context.sh — replace FreeSWITCH's stock "default" dialplan
# context with the repo's deny-all one (scripts/freeswitch/default_context.xml).
#
# The stock context is a demo dialplan whose extensions reach every call on
# the switch: 779 eavesdrop all, 886 / *8 / **<ext> intercept, park, and
# conference rooms shared by all tenants. The stock `public` context hands
# 10xx, 35xx-38xx and 5551212 on to it, so any call that reaches FreeSWITCH's
# external profile can land there. Nothing on this platform routes to
# `default` — transfers and originates bridge inline — so it is safe to close.
#
# Used by setup_macos.sh, and run by hand on a Linux/VM install
# (docs/telephony-vm-setup.md §6). Idempotent: the stock file is kept once as
# default.xml.stock and never overwritten.
#
# Usage: scripts/freeswitch/install_default_context.sh <freeswitch-conf-dir>
#   e.g. ~/.yuviz/freeswitch/conf (macOS) or /etc/freeswitch (Debian apt)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${1:?usage: $0 <freeswitch-conf-dir>}"
DIALPLAN="$CONF/dialplan"

if [[ ! -d "$DIALPLAN" ]]; then
  echo "ERROR: $DIALPLAN not found — pass FreeSWITCH's conf dir" >&2
  exit 1
fi

if [[ -f "$DIALPLAN/default.xml" && ! -f "$DIALPLAN/default.xml.stock" ]] &&
   ! grep -q 'name="deny_all"' "$DIALPLAN/default.xml"; then
  mv "$DIALPLAN/default.xml" "$DIALPLAN/default.xml.stock"
fi
cp "$HERE/default_context.xml" "$DIALPLAN/default.xml"
echo "  dialplan: default context replaced with deny-all (stock kept as default.xml.stock)"
