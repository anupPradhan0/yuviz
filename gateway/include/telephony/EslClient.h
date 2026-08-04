#pragma once

#include "config/Config.h"
#include "logging/Logger.h"
#include "telephony/TransferRequest.h"

#include <mutex>
#include <string>

namespace voiceai {

// Minimal FreeSWITCH Event Socket Library (inbound mode) client.
//
// Speaks ESL's plain-text protocol directly over a blocking TCP socket — no
// full ESL SDK dependency for what is, today, a single command (uuid_kill).
// One persistent connection, serialized by a mutex: ESL's request/reply
// protocol processes one command at a time per connection, so concurrent
// hangups from different sessions' control threads share this one
// connection safely but not concurrently.
//
// Disabled gracefully: if cfg.enabled is false, or the connection/command
// fails for any reason, hangup() logs a warning and returns — it never
// throws and never blocks the caller beyond roughly 2x cfg.connect_timeout_ms
// (one connect + one command round trip).
class EslClient {
public:
    EslClient(EslConfig cfg, Logger& logger);
    ~EslClient();

    EslClient(const EslClient&)            = delete;
    EslClient& operator=(const EslClient&) = delete;

    // Hang up the FreeSWITCH channel identified by `uuid` (the same UUID
    // carried as SessionContext::obs.call_id, sourced from mod_audio_fork's
    // callSid — see CallSession::on_text_message).  Safe to call from any
    // thread; connects lazily and reconnects on failure.  No-op if
    // cfg.enabled is false or uuid is empty.
    void hangup(const std::string& uuid, const std::string& reason);

    // Cold-transfer the FreeSWITCH channel identified by `req.call_id` to
    // `req.destination` — either a plain phone number/extension (routed
    // through FreeSWITCH's default XML dialplan, e.g. "1005") or a SIP URI
    // ("sip:"/"sips:" prefix, bridged directly via mod_sofia's external
    // profile using the "inline" dialplan, which needs no matching dialplan
    // extension to exist for the destination).
    //
    // Cold only: this redirects the channel and does not bridge/three-way
    // anything — the AI leg is expected to already be torn down by the
    // caller (see CallSession), not kept alive alongside the transfer.
    //
    // IMPORTANT — what the return value actually means: a `true` return (a
    // "+OK" reply) means FreeSWITCH *accepted* the uuid_transfer command,
    // nothing more. uuid_transfer is asynchronous — acceptance says nothing
    // about whether the destination actually answers, is busy, rejects, or
    // times out. Do NOT treat this return value as "the transfer
    // succeeded." The real outcome arrives later as a CHANNEL_BRIDGE
    // (success) or CHANNEL_HANGUP (failure) event — see
    // EslEventListener/TransferCorrelator, which CallSession uses to learn
    // the actual result and only then completes CallFSM's Transferring
    // state. A `false` return means the command itself was never even
    // accepted (ESL unreachable, disabled, rejected, or a bad argument) —
    // in that case no async event will ever arrive for this attempt, so the
    // caller should treat it as an immediate, final failure.
    //
    // Blocking: waits for ESL's command reply (up to ~cfg.connect_timeout_ms)
    // before returning, same as hangup() — safe to call from any thread.
    // false is accompanied by a machine-readable reason in `error_out`
    // ("esl_disabled", "empty_uuid", "empty_destination", "esl_unreachable",
    // or FreeSWITCH's own -ERR reply text). Never throws.
    bool transfer(const TransferRequest& req, std::string& error_out);

    // ── Warm transfer primitives (see docs/warm_transfer_architecture.md §6) ──
    // Unlike transfer() above, none of these redirect/consume the caller's
    // own channel — they operate on a second, independently-originated
    // "agent leg" (or, for hold/unhold, on the caller's leg without ending
    // its connection to the Gateway).

    // Originates a new, independent channel to `destination` — via `bgapi`,
    // never blocking on ring/answer (a blocking `api originate` would hold
    // this single ESL connection hostage for the full ring duration,
    // starving every other session's ESL commands). Returns true and fills
    // `out_job_uuid` with FreeSWITCH's own Job-UUID for this async command
    // once accepted; the real outcome (answered/busy/rejected/no-answer)
    // arrives later as a BACKGROUND_JOB event carrying that Job-UUID,
    // followed by CHANNEL_ANSWER/CHANNEL_HANGUP on the new leg's own uuid —
    // see EslEventListener/TransferContextRegistry, which the caller uses
    // to correlate both. `caller_id_number`: what the originated leg's
    // caller ID shows (v1: always the original caller's own ANI — see
    // docs/warm_transfer_architecture.md's CallerIdPolicy discussion).
    // false means the command itself was never accepted — no BACKGROUND_JOB
    // will ever arrive for it, same "immediate final failure" contract as
    // transfer(). Blocking only for the command's own (fast) accept/reject
    // reply, not for the leg to ring/answer.
    bool originate_async(const std::string& destination, const std::string& caller_id_number,
                        std::string& out_job_uuid, std::string& error_out);

    // Bridges two independently-existing channels together — issued only
    // after the agent leg's CHANNEL_ANSWER, never before. This is the
    // moment control of the caller's audio leaves the Gateway/AI pipeline
    // for good (media_owner: BridgePending → FreeSwitch — see
    // docs/warm_transfer_architecture.md §3).
    bool bridge(const std::string& uuid_a, const std::string& uuid_b, std::string& error_out);

    // Detaches the audio fork from `uuid` (the caller's own leg) —
    // mod_audio_fork's media bug otherwise survives being bridged to a
    // different leg (see docs/warm_transfer_architecture.md §6's media-
    // ownership note), so this MUST be called, and its reply MUST be
    // observed, before bridge() — sequential, not concurrent, or a window
    // exists where the AI pipeline still receives live audio of what is
    // about to become a human-to-human call.
    bool stop_audio_fork(const std::string& uuid, std::string& error_out);

    // Hold/MOH for the caller's leg while the agent leg rings — kept
    // distinct from send_playback_finished/the Gateway's own playback
    // queue; this is FreeSWITCH-side hold, not Gateway-synthesized audio.
    // NOTE: interaction with an already-attached uuid_audio_fork on the
    // same leg is not yet live-verified — see
    // docs/warm_transfer_architecture.md open item 1 before relying on
    // this for the "resume AI cleanly after cancel" path.
    bool hold(const std::string& uuid, std::string& error_out);
    bool unhold(const std::string& uuid, std::string& error_out);

private:
    bool ensure_connected_locked();
    bool send_command_locked(const std::string& command, std::string& reply_out);
    void disconnect_locked();

    EslConfig cfg_;
    Logger&   logger_;

    std::mutex  mutex_;
    int         fd_{-1};   // -1 = not connected
    std::string read_buf_; // bytes read past the last parsed reply, carried to the next read
};

} // namespace voiceai
