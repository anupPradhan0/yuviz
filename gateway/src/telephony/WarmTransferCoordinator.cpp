#include "telephony/WarmTransferCoordinator.h"

namespace voiceai {

WarmTransferCoordinator::WarmTransferCoordinator(
    EslClient& esl_client, TransferCorrelator& leg_correlator,
    TransferCorrelator& job_correlator, ContextLogger& log)
    : esl_client_(esl_client)
    , leg_correlator_(leg_correlator)
    , job_correlator_(job_correlator)
    , log_(log)
{}

void WarmTransferCoordinator::start(TransferCoordinatorContext ctx,
                                    TransferCoordinatorCallbacks callbacks) {
    state_              = CoordinatorState::Active;
    media_owner_        = MediaOwnership::Gateway;
    customer_uuid_      = ctx.call_id;
    destination_        = ctx.destination;
    transfer_id_        = ctx.transfer_id;
    caller_id_          = ctx.caller_id;
    waiting_experience_ = ctx.waiting_experience;
    agent_uuid_.clear();
    job_uuid_.clear();
    callbacks_      = std::move(callbacks);

    // Hold the customer's leg — the AI has already finished speaking the
    // transfer_announcement by the time this runs (see servicer.py/
    // pipeline.py's hold-until-playback-finishes ordering); the caller now
    // hears FreeSWITCH's own MOH while the agent leg rings. Skipped
    // entirely for "announcement_silence" (agent.transfer_waiting_
    // experience): the caller just hears silence instead of MOH — nothing
    // else about the sequence changes, since mod_audio_fork has nothing
    // left to send either way once the announcement finishes. NOT gated on
    // success in the MOH case: a failed hold degrades to silence, not a
    // broken transfer.
    if (waiting_experience_ != "announcement_silence") {
        std::string hold_error;
        if (!esl_client_.hold(customer_uuid_, hold_error)) {
            log_.warn("Warm transfer: hold failed uuid={} error={} transfer_id={} — "
                      "continuing without MOH", customer_uuid_, hold_error, transfer_id_);
        }
    }

    std::string error;
    if (!esl_client_.originate_async(destination_, caller_id_, job_uuid_, error)) {
        log_.warn("Warm transfer: originate not accepted destination={} error={} "
                 "transfer_id={}", destination_, error, transfer_id_);
        finish(false, destination_, error);
        return;
    }

    // Registered immediately upon learning job_uuid_ — this is the
    // earliest possible moment, since (unlike cold's call_id, known before
    // the command is even issued) bgapi's Job-UUID only exists once
    // originate_async()'s own synchronous reply returns it. A BACKGROUND_JOB
    // arriving in the microseconds between that reply and this watch()
    // call is not realistically possible given how long an actual ring/
    // answer sequence takes, but this ordering is inherent to bgapi, not a
    // gap this implementation could close further.
    job_correlator_.watch(job_uuid_, [this](bool success, std::string result) {
        on_job_resolved(success, std::move(result));
    });
}

void WarmTransferCoordinator::on_job_resolved(bool success, std::string result) {
    // FreeSWITCH's `originate` does not return successfully until the
    // destination actually answers (or the trailing app — here &park() —
    // begins running, which only happens post-answer) — so a successful
    // BACKGROUND_JOB result already IS the answer confirmation, not merely
    // "the command was accepted." This is a deliberate simplification
    // versus separately watching CHANNEL_ANSWER on the agent leg: that
    // leg's uuid isn't knowable until THIS exact moment (it's the result
    // text itself on success), by which point any CHANNEL_ANSWER for it
    // has already fired — there is no practical window left to watch it
    // in. EslEventListener still subscribes to CHANNEL_ANSWER (see its own
    // doc comment) for observability/future use, but this coordinator does
    // not gate on it.
    //
    // Live-confirmed on the first end-to-end warm-transfer test call, with
    // an important caveat that shaped EslClient::originate_async()'s dial-
    // string choice: this assumption only holds when the dial string names
    // the real destination directly (a SIP URI, or a plain extension dialed
    // straight at the deployment's SIP proxy/registrar). Two earlier
    // versions got this wrong — see originate_async()'s own comment for the
    // full history — both because they let something OTHER than the real
    // destination's own answer state gate when &park() (and therefore this
    // callback) fires: a loopback pairing's own connect, or FreeSWITCH's
    // directory answering for a proxy-registered device it never actually
    // saw. Either way, this coordinator ended up stopping the audio fork
    // and bridging the customer to a dead leg before the real failure
    // surfaced.
    if (!success) {
        log_.warn("Warm transfer: originate failed destination={} detail={} transfer_id={}",
                 destination_, result, transfer_id_);
        // Confirmed live (2026-07-21): start() puts the customer on hold
        // before the agent leg is even dialed, but this branch used to
        // return straight to finish() without ever undoing that — the
        // caller stayed on MOH indefinitely (never even hearing the
        // apology TTS Python generates in response to TransferFailed,
        // since a held channel doesn't play newly-arriving audio over the
        // hold state) whenever the agent leg was busy, rejected the call,
        // or never answered. Unconditional, like start()'s own hold()
        // call: a failed unhold degrades to the caller staying on MOH a
        // little longer, not a crash — logged loudly either way.
        std::string unhold_error;
        if (!esl_client_.unhold(customer_uuid_, unhold_error)) {
            log_.warn("Warm transfer: unhold failed uuid={} error={} transfer_id={} — "
                      "caller may remain on hold", customer_uuid_, unhold_error, transfer_id_);
        }
        finish(false, destination_, result.empty() ? "no_answer" : result);
        return;
    }

    agent_uuid_ = std::move(result);
    log_.info("Warm transfer: agent leg answered agent_uuid={} destination={} transfer_id={}",
             agent_uuid_, destination_, transfer_id_);

    std::string unhold_error;
    if (!esl_client_.unhold(customer_uuid_, unhold_error)) {
        log_.warn("Warm transfer: unhold failed uuid={} error={} transfer_id={} — "
                  "proceeding anyway", customer_uuid_, unhold_error, transfer_id_);
    }

    // Sequential media-ownership handoff (see docs/warm_transfer_
    // architecture.md §6/"Media ownership"): stop_audio_fork()'s own
    // command reply MUST be observed before bridge() is issued — a media
    // bug survives being bridged to a different leg unless explicitly
    // stopped first, so racing or skipping this leaks live human-to-human
    // audio into the AI pipeline. Gateway → BridgePending happens now,
    // regardless of whether the stop command itself succeeds (a failed
    // stop is logged loudly but does not abort an otherwise-successful
    // transfer — the alternative, aborting a transfer the agent has
    // already answered because of a fork-detach hiccup, is strictly worse).
    //
    // on_media_handoff fires BEFORE stop_audio_fork(), not after: stopping
    // the fork is what makes mod_audio_fork close its WebSocket connection
    // to the Gateway, and CallSession needs its "don't hang up on
    // disconnect" guard set before that disconnect can possibly arrive —
    // see TransferCoordinatorCallbacks::on_media_handoff's own comment.
    media_owner_ = MediaOwnership::BridgePending;
    if (callbacks_.on_media_handoff) callbacks_.on_media_handoff();
    std::string fork_error;
    if (!esl_client_.stop_audio_fork(customer_uuid_, fork_error)) {
        log_.warn("Warm transfer: stop_audio_fork failed uuid={} error={} transfer_id={} — "
                  "bridging anyway (AI may briefly receive post-bridge audio)",
                  customer_uuid_, fork_error, transfer_id_);
    }

    std::string bridge_error;
    if (!esl_client_.bridge(customer_uuid_, agent_uuid_, bridge_error)) {
        log_.warn("Warm transfer: bridge failed customer_uuid={} agent_uuid={} error={} "
                 "transfer_id={}", customer_uuid_, agent_uuid_, bridge_error, transfer_id_);
        esl_client_.hangup(agent_uuid_, "bridge_failed");
        finish(false, destination_, "bridge_failed:" + bridge_error);
        return;
    }

    media_owner_ = MediaOwnership::FreeSwitch;
    finish(true, destination_, "bridged");
}

void WarmTransferCoordinator::cancel() {
    if (state_ != CoordinatorState::Active) return;

    log_.info("Warm transfer: cancelled agent_uuid={} job_uuid={} transfer_id={}",
             agent_uuid_, job_uuid_, transfer_id_);

    // Abort whichever stage is in flight. If the agent leg already exists
    // (BACKGROUND_JOB already resolved but bridge() hasn't run yet — a
    // narrow window), kill it; otherwise there is nothing to kill yet and
    // the eventual BACKGROUND_JOB result is simply left unhandled (the
    // watch is removed by shutdown()/finish() below, so it resolves into
    // nothing when it does arrive).
    if (!agent_uuid_.empty()) {
        esl_client_.hangup(agent_uuid_, "transfer_cancelled");
    }
    std::string unhold_error;
    esl_client_.unhold(customer_uuid_, unhold_error);

    finish(false, destination_, "cancelled");
}

void WarmTransferCoordinator::finish(bool success, std::string destination, std::string detail) {
    state_ = CoordinatorState::Completed;
    if (!job_uuid_.empty()) job_correlator_.cancel(job_uuid_);
    if (!agent_uuid_.empty()) leg_correlator_.cancel(agent_uuid_);
    if (callbacks_.on_transfer_completed)
        callbacks_.on_transfer_completed(success, std::move(destination), std::move(detail));
}

void WarmTransferCoordinator::shutdown() {
    if (state_ == CoordinatorState::Idle) return;
    // Idempotent, and deliberately does NOT call finish()/fire
    // on_transfer_completed — see ITransferCoordinator.h's contract: by
    // the time CallSession calls this from ~CallSession() (the teardown-
    // mid-transfer case), the session is being destroyed and no further
    // callback into it is safe to make at all.
    if (!job_uuid_.empty()) job_correlator_.cancel(job_uuid_);
    if (!agent_uuid_.empty()) leg_correlator_.cancel(agent_uuid_);
    state_ = CoordinatorState::Completed;
}

CoordinatorState WarmTransferCoordinator::state() const noexcept {
    return state_;
}

} // namespace voiceai
