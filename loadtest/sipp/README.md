# SIPp concurrency load test

Real RTP audio injected against a real Kamailio → FreeSWITCH → Gateway →
Conversation Service call, simulating an actual human caller — not a
synthetic SIP-only ping.

## What it simulates

`uac.xml` drives a 3-turn conversation via `combined_audio.pcap`:

```
0-6s    silence  (human listens to the bot's greeting)
6-7.5s  speech   "My name is Rahul"
7.5-17s silence  (STT + LLM + TTS + bot speaks)
17-19s  speech   "I have a billing issue"
19-29s  silence  (STT + LLM + TTS + bot speaks)
29-30s  speech   "Thank you goodbye"
30-31s  silence  (trailing before BYE)
```

Real INVITE/ACK/BYE, real RTP media negotiation — this exercises the
actual pipeline a phone call would, including STT/LLM/TTS turnaround
time built into the pauses.

## Prerequisites

- SIPp with PCAP support (`brew install sipp` — confirmed
  `v3.7.7-TLS-PCAP-SHA256`, `sipp -v` to check yours)
- The full local stack running (`scripts/start_local.sh` — Kamailio,
  FreeSWITCH, Gateway, Conversation Service, Envoy, Redis, Postgres)
- A DID in Kamailio's routable range (`500[0-9]` — see
  `kamailio_did_routing_gotcha` in project memory) assigned to a real
  agent. Defaults to `5000`.
- `combined_audio.pcap` is not committed here yet — pull it from the
  original source and drop it in this directory:
  ```bash
  sudo cp /opt/src/testSimulator/combined_audio.pcap .
  sudo chown $(whoami) combined_audio.pcap
  ```

## Usage

```bash
./run_human_sim.sh [cps] [concurrent] [total] [--monitor]
```

- `cps` — call rate (default `1`)
- `concurrent` — max calls in flight at once (default `3`)
- `total` — total calls to place (default `20`)
- `--monitor` — also sample CPU/RSS every 2s for the Gateway, both
  Conversation Service instances, FreeSWITCH, and Kamailio for the
  duration of the run. **Use this** — without it you only get
  SIP-level pass/fail, which doesn't answer G1.

Target IP is auto-detected the same way `scripts/update_kamailio_ip.sh`
does (not hardcoded) — override with `KAMAILIO_IP=x.x.x.x` if needed.
Test DID overrides via `SIPP_TEST_DID=5001` etc.

**Start small.** Don't jump straight to 50 concurrent — try `10` or
`15` first, confirm nothing falls over, then step up. Each run writes
to a timestamped `results/<date>/` directory:

```
results/20260818_143000/
├── stats.csv               call success/failure, response times (SIPp's own output)
├── run_summary.log         this script's own banner/setup messages
├── uac_<pid>_screens.log   SIPp's live-screen snapshots over time (-trace_screen)
├── uac_<pid>_messages.log  full SIP message trace (-trace_msg)
├── uac_<pid>_errors.log    only present if something actually went wrong
└── resource_usage.csv      CPU%/RSS per process, sampled every 2s (only with --monitor)
```

SIPp's own output is intentionally **not** piped through a logger — SIPp's
live-updating screen (call rate, current calls, message counters) only
renders when stdout is a real terminal; piping it (even through `tee`)
makes SIPp silently fall back to a one-shot final summary, killing the
live view. `-trace_screen` gets the same information into a file instead,
without touching SIPp's own stdout.

## Reading `resource_usage.csv`

One row per process per 2s sample: `timestamp,process,pid,cpu_pct,rss_mb`.
Watch `conversation` specifically — that's the STT/LLM/TTS pipeline,
and the actual answer to G1 is what its CPU/RSS looks like as
concurrent call count climbs. If it's still healthy at 25 concurrent
(50 total split across the two Conversation Service instances behind
Envoy), G1 closes as "verified, not a blocker." If it isn't, this data
tells you where the real ceiling is instead of guessing.

## Known gaps in this harness

- `results/` is gitignored — raw run output, not source, don't commit it
- No caller-side audio *quality* verification (does the bot's answer
  actually sound right) — this measures capacity and call completion,
  not conversational correctness
- The original one-time run this was adapted from (`/opt/src/testSimulator`,
  2026-07-08) predates several changes this session (Vault migration,
  provider config changes, the barge-in/TTS fix) — treat any of its
  historical numbers as stale; only new runs against the current stack
  count for G1
