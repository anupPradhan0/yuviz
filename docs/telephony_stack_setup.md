# Telephony Stack Setup — Kamailio + FreeSWITCH + Gateway (Native, macOS)

> This covers the piece `docs/setup.md` explicitly excludes: real SIP call
> handling. That doc's web-testing path (Config/Knowledge/Conversation
> Services, admin-ui's browser Test Agent) is a prerequisite — do that
> first. This document was written by reading the actual working install on
> one real machine, not reconstructed from memory — every path, config
> value, and file below was confirmed against a live, running instance.

**Run `scripts/setup_telephony_stack.sh` instead of doing this by hand.**
It automates everything below — Homebrew installs, getting FreeSWITCH
installed with `vendor/mod_audio_fork` wired in, deploying the
dialplan/Lua/SIP profile/ESL/modules config into the real CONFDIR, and
bootstrapping Kamailio's MySQL schema + subscriber + rendered config. It's
idempotent — safe to re-run after fixing a step by hand.

On Apple Silicon, FreeSWITCH itself is **downloaded prebuilt** (a
[GitHub Release](https://github.com/yuviz-ai/yuviz/releases/tag/freeswitch-prebuilt-8babcee3-arm64)
built once from the exact pinned commit + the patched `mod_audio_fork` —
FreeSWITCH's own source has no local changes, so there's nothing
machine-specific about the compiled output) instead of compiling from
source, which otherwise takes 20-60+ minutes per machine. It falls back to
a source build automatically on non-arm64 machines, if the release asset
is unreachable, or with `SKIP_PREBUILT=1`. **If FreeSWITCH itself ever
needs a local patch, rebuild and re-publish that release** — the tarball
would otherwise go stale silently. The rest of this document is the
reference for what the script does and why, and the fallback if you need
to do a step manually.

## 1. Architecture — how a call actually flows

```
Caller --SIP--> Kamailio (5060) --dispatch--> FreeSWITCH (5080)
                                                    |
                                          mod_audio_fork (Lua-triggered)
                                                    |
                                          WebSocket ws://127.0.0.1:8080/voice/<uuid>
                                                    |
                                              C++ Gateway (8080)
                                                    |
                                          gRPC (direct :50051/2, or via Envoy :10000)
                                                    |
                                          Conversation Service (STT -> LLM -> TTS)
```

Kamailio and FreeSWITCH do **pure telephony** — SIP signaling and media.
Neither one knows which tenant or agent owns a DID. The Gateway resolves
that itself via Redis (`PhoneRoute::from_redis("did:" + destination_number)`)
on every call — this is why FreeSWITCH's dialplan is a single generic
extension, never edited again when a tenant buys a number or changes
agents (see the comment in `voice_ai.xml` below, which explains this
choice — a `mod_xml_curl` dynamic dialplan was considered and rejected for
exactly this reason).

## 2. Components to install

| Component | Purpose | Confirmed port(s) |
|---|---|---|
| MySQL | Kamailio's `dispatcher`/`subscriber` tables | 3306 |
| Kamailio | SIP proxy — routes DIDs to FreeSWITCH | 5060 |
| FreeSWITCH | SIP UA + media, with `mod_audio_fork` + `mod_lua` | 5080 (SIP), 8021 or 8022 (ESL — confirmed 8022 on this box) |
| C++ Gateway (this repo's `gateway/`) | Bridges FreeSWITCH media to the Conversation Service | 8080 (WebSocket), 9090 (metrics) |

Install Kamailio and MySQL via your platform's normal path (Homebrew on
macOS: `brew install kamailio mysql`). FreeSWITCH must be built from
source, pinned at the exact commit confirmed running on the existing box:

```bash
git clone https://github.com/signalwire/freeswitch.git
cd freeswitch
git checkout 8babcee3eaab4d8b0b60c2a113386419b86bc9d9
```

`mod_audio_fork` is a third-party module, not stock FreeSWITCH, and the
copy running here carries a local patch that upstream doesn't have (see
`vendor/mod_audio_fork/PROVENANCE.md` for exactly what changed and why —
without it, TTS can't play back to the caller, only STT capture works).
Use the source vendored in this repo rather than pulling upstream fresh:

```bash
cp -r vendor/mod_audio_fork/src <freeswitch-src>/src/mod/applications/mod_audio_fork
# follow vendor/mod_audio_fork/files/*.extra and *.patch to wire it into
# configure.ac / modules.conf.in, then build it as part of the normal
# FreeSWITCH module build.
```

**If you already have a working FreeSWITCH build with `mod_audio_fork`
and `mod_lua`, everything below assumes that starting point.**

Confirm both required modules are actually loaded once FreeSWITCH is
running:

```bash
fs_cli -x "module_exists mod_audio_fork"   # must print true
fs_cli -x "module_exists mod_lua"          # must print true
```

If either prints `false`, check `autoload_configs/modules.conf.xml` (see
§4 for the real path) has a `<load module="mod_audio_fork"/>` /
`<load module="mod_lua"/>` line, and that the module actually built.

## 3. A critical gotcha: FreeSWITCH's real config directory

**Do not assume `/usr/local/freeswitch/conf/` is FreeSWITCH's active
config.** On this build, that directory was a stale, unused skeleton left
over from install — FreeSWITCH's actual compiled-in `CONFDIR` is
**`/usr/local/freeswitch/etc/freeswitch/`**. Confirm which one your build
actually uses before editing anything:

```bash
grep CONFDIR /usr/local/freeswitch/bin/fsxs
```

If it prints `etc/freeswitch`, use that path for everything in §4. Every
path below assumes this same layout — if your FreeSWITCH build differs
(e.g. genuinely uses `conf/`), adjust accordingly, but verify first via the
command above rather than guessing from FreeSWITCH's own documentation
(which often assumes the older `conf/`-only layout).

## 4. FreeSWITCH configuration to replicate

All paths below are relative to the real `CONFDIR` from §3
(confirmed `/usr/local/freeswitch/etc/freeswitch/` on the reference
machine).

### 4a. The dialplan extension

`dialplan/public/voice_ai.xml` — the single, permanent, generic entry
point for every AI-routed call:

```xml
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
```

The reference machine also had a one-off POC extension for a bare `788`
test number (`dialplan/public/ai_stt_poc.xml`, using a different, older
`start_stt.lua` script) — not needed for a fresh setup, only mentioned here
so it doesn't look mysterious if you see it referenced anywhere.

**Ordering matters**: this is a catch-all regex (`^\+?[0-9]{4,15}$`). Any
other extension matching a more specific number (e.g. a literal `5551212`
test extension) must come earlier in file-load order (FreeSWITCH loads
`public/*.xml` alphabetically) and must not have `continue="true"`, or the
generic voice-ai extension will shadow it.

### 4b. The Lua script

`share/freeswitch/scripts/start_voice_ai.lua` (confirmed: this is **not**
under `etc/freeswitch/scripts/` — `mod_lua`'s script search path resolves
to the `share/freeswitch/scripts/` directory on this build; verify yours
the same way, by checking where an existing working script lives):

```lua
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
```

This is the entire bridge: it starts `mod_audio_fork` streaming mono
16kHz audio to the Gateway's WebSocket at `ws://127.0.0.1:8080/voice/<uuid>`,
with a JSON metadata frame (call ID, DID, ANI, direction) sent once before
any audio. The Gateway's C++ side never issues the `start` — only
`stop` (`api uuid_audio_fork <uuid> stop`, confirmed in
`gateway/src/telephony/EslClient.cpp`), so this script is the one and only
place a call actually gets attached to the AI pipeline.

### 4c. SIP profile

`sip_profiles/external.xml` must have:

```xml
<param name="dialplan" value="XML"/>
<param name="context" value="public"/>
```

This is what routes inbound SIP traffic on the external profile (port
5080) into the `public` dialplan context, where `voice_ai.xml` lives.

### 4d. Event Socket (ESL) — Kamailio and the Gateway both need it

`autoload_configs/event_socket.conf.xml`:

```xml
<configuration name="event_socket.conf" description="Socket Client">
  <settings>
    <param name="listen-ip" value="0.0.0.0"/>
    <param name="listen-port" value="8022"/>
    <param name="password" value="ClueCon"/>
    <param name="apply-inbound-acl" value="loopback.auto"/>
  </settings>
</configuration>
```

(Confirmed live on port **8022**, not FreeSWITCH's more commonly-documented
default of 8021 — check yours with `fs_cli -x status` or by testing both
ports; `scripts/update_kamailio_ip.sh` already assumes 8022 with password
`ClueCon`, matching this.)

### 4e. Required modules

`autoload_configs/modules.conf.xml` needs both of these `<load>` lines
present (order doesn't matter relative to each other):

```xml
<load module="mod_audio_fork"/>
<load module="mod_lua"/>
```

## 5. Kamailio configuration

This repo already versions the real templates — do not hand-write a
`kamailio.cfg` from scratch:

```bash
scripts/kamailio/kamailio.cfg.tpl        # __LAN_IP__ placeholder
scripts/kamailio/dispatcher.list.tpl     # __LAN_IP__ placeholder
```

### 5a. Bootstrap the MySQL schema (once, on a fresh machine)

Kamailio's `subscriber`/`dispatcher`/etc. tables are **not** versioned in
this repo — they come from Kamailio's own standard bootstrapping tool:

```bash
kamdbctl create
```

(Follow Kamailio's own prompts — this creates the standard schema its
`kamailio.cfg` expects, including `subscriber` and `dispatcher`.)

### 5b. Generate the real config from the templates

```bash
scripts/update_kamailio_ip.sh
```

This is not just for realigning after a network change — on a fresh
machine with no `/usr/local/etc/kamailio/kamailio.cfg` yet, it will still
detect your LAN IP, render both templates with `__LAN_IP__` substituted,
and write them to `/usr/local/etc/kamailio/`. Read the script's own header
comment — it also explains why three independent things (Kamailio's
config, the MySQL `subscriber` table's `domain`/`ha1`/`ha1b` columns, and
FreeSWITCH's `local_ip_v4`) all have to move together whenever the LAN IP
changes, which will happen the first time you run this on a new laptop.

### 5c. Add a subscriber row for FreeSWITCH's registration

The templates + `update_kamailio_ip.sh` assume at least one `subscriber`
row already exists (it only *updates* stale domains, doesn't create rows
from nothing). Use Kamailio's own `kamctl` for this:

```bash
kamctl add <username> <password>
```

Match whatever username/password `kamailio.cfg.tpl` expects FreeSWITCH to
register as — check the template for the exact value before picking one.

## 6. Starting everything

Once Kamailio, FreeSWITCH (with the config above), and MySQL are all in
place:

```bash
source scripts/start_local.sh
```

Then, in order, in separate terminal tabs:

```bash
start_mysql
start_kamailio
start_data        # Postgres + Redis
start_freeswitch
start_gateway      # build it first: cd gateway && cmake -B ../build/gateway && cmake --build ../build/gateway
start_envoy        # or skip and point Gateway/tests directly at :50051
start_conv1
```

Run `verify` (from the same sourced script) to check every port, and
`./scripts/verify_setup.sh` for actual health checks (not just port
connectivity).

## 7. Verifying a real call end-to-end

1. Register a SIP softphone (e.g. Zoiper, Linphone) against Kamailio using
   the subscriber credentials from §5c.
2. Dial any 4-15 digit number (the dialplan's catch-all matches
   anything — an unprovisioned DID still reaches the Gateway and falls
   back to the `default` tenant/agent, so this never fails with "no
   route").
3. Watch for the log line confirming the bridge fired:
   ```bash
   fs_cli -x "console loglevel debug"
   # then, in another tab, tail the console and place the call —
   # look for: "=== Attaching mod_audio_fork to Voice AI uuid=... ==="
   # followed by: "=== LUA STT started uuid=... result=... ==="
   ```
4. If both log lines appear but there's no audio, check the Gateway's own
   log for a WebSocket connection on `:8080/voice/<uuid>` and the
   Conversation Service's log for a new gRPC session — that narrows
   whether the break is FreeSWITCH-side or Gateway-side.

## 8. What this document does not cover

- Kamailio's own installation (assumed via your platform's package
  manager or a source build).
- The C++ Gateway's own dependencies beyond `cmake --build` — see
  `gateway/CMakeLists.txt` for what it links against.
