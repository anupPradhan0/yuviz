# mod_audio_fork — provenance

This is a vendored copy of the FreeSWITCH module `mod_audio_fork`, which
the Gateway depends on for the SIP-native call path (see
`docs/telephony_stack_setup.md`). It is not a stock FreeSWITCH module —
it's a third-party module with a local patch, and the patch is the only
thing that makes bidirectional audio (TTS playback back to the caller)
work. Vendoring it here is the backup for that patch; it does not replace
building/installing it on the FreeSWITCH host per
`docs/telephony_stack_setup.md`.

## Upstream

- Repo: https://gitee.com/gridsoft/mod_audio_fork.git
- Pinned commit: `e6bb39f0cd7a9427d050b92b647cd2391aaf7955` (2023-03-01,
  "chore: add libs")

## Local patch on top of upstream

Commit `4e40fb8` in the module's own local git history (on the machine
this was vendored from — that history is not part of this repo, only the
resulting source is). 6 files, +179/-19:

- `src/mod_audio_fork.c`, `src/mod_audio_fork.h`
- `src/audio_pipe.cpp`, `src/audio_pipe.hpp`
- `src/lws_glue.cpp`, `src/lws_glue.h`

Adds a `SWITCH_ABC_TYPE_WRITE_REPLACE` handler (`fork_write_frame`) and
sets `flags = SMBF_READ_STREAM | SMBF_WRITE_REPLACE` (upstream only sets
`SMBF_READ_STREAM`). Without this patch the module only captures audio
for STT; it cannot push synthesized TTS audio back into the call leg.

## FreeSWITCH itself (not vendored)

FreeSWITCH core is unmodified official upstream —
https://github.com/signalwire/freeswitch.git, pinned at
`8babcee3eaab4d8b0b60c2a113386419b86bc9d9` (2026-04-02). Don't vendor it;
just `git clone` + `git checkout` that commit per
`docs/telephony_stack_setup.md`.

## Build

Drop `src/` into `<freeswitch-src>/src/mod/applications/mod_audio_fork/`
and build via FreeSWITCH's normal module build (see
`docs/telephony_stack_setup.md` for the full local setup sequence). The
`files/*.extra` and `*.patch` files are upstream's own instructions for
wiring this module into FreeSWITCH's `configure.ac`/`modules.conf.in` —
unmodified from upstream.
