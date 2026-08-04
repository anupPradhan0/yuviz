#include "telephony/ColdTransferCoordinator.h"

namespace voiceai {

ColdTransferCoordinator::ColdTransferCoordinator(
    EslClient& esl_client, TransferCorrelator& correlator, ContextLogger& log)
    : esl_client_(esl_client), correlator_(correlator), log_(log)
{}

void ColdTransferCoordinator::start(TransferCoordinatorContext ctx,
                                    TransferCoordinatorCallbacks callbacks) {
    state_           = CoordinatorState::Active;
    active_call_id_  = ctx.call_id;
    callbacks_       = std::move(callbacks);

    // Registered *before* issuing the command: CHANNEL_BRIDGE/CHANNEL_HANGUP
    // arrive on EslEventListener's independent connection and thread, with
    // no ordering guarantee relative to this command's own reply — a fast
    // event could otherwise arrive before transfer() returns.
    correlator_.watch(ctx.call_id,
        [this, destination = ctx.destination](bool success, std::string detail) {
            state_ = CoordinatorState::Completed;
            if (success) {
                log_.info("Transfer confirmed destination={} detail={}", destination, detail);
            } else {
                log_.warn("Transfer failed destination={} detail={}", destination, detail);
            }
            if (callbacks_.on_transfer_completed)
                callbacks_.on_transfer_completed(success, destination, std::move(detail));
        });

    TransferRequest req{ctx.call_id, "cold", ctx.destination, ctx.reason, ctx.transfer_id};
    std::string error;
    const bool accepted = esl_client_.transfer(req, error);
    if (accepted) {
        // uuid_transfer moves the customer's own channel to a new dialplan
        // context — FreeSWITCH may tear down mod_audio_fork's media bug as
        // a side effect, closing its WebSocket connection to the Gateway,
        // before CHANNEL_BRIDGE ever confirms the outcome. See
        // TransferCoordinatorCallbacks::on_media_handoff's own comment.
        if (callbacks_.on_media_handoff) callbacks_.on_media_handoff();
    } else {
        // The command itself was never accepted (ESL unreachable, disabled,
        // or rejected outright) — no async event will ever arrive for this
        // attempt. Cancel the watch (it would otherwise sit unresolved
        // until CallFSM's TransferTimeout, needlessly delaying an outcome
        // already known) and resolve now.
        correlator_.cancel(ctx.call_id);
        state_ = CoordinatorState::Completed;
        log_.warn("Transfer command not accepted destination={} error={} transfer_id={}",
                 ctx.destination, error, ctx.transfer_id);
        if (callbacks_.on_transfer_completed)
            callbacks_.on_transfer_completed(false, ctx.destination, std::move(error));
    }
    // else: accepted — wait for the watch's callback (event-driven) or
    // CallFSM's own TransferTimeout, whichever comes first.
}

void ColdTransferCoordinator::cancel() {
    // Cold has no pre-dispatch "hold" phase to abort out of — by the time
    // start() returns, uuid_transfer has already been issued (or
    // immediately rejected, already resolved). uuid_transfer is
    // fire-and-forget once accepted (see EslClient::transfer()'s own doc
    // comment) — there is nothing safe to abort. No-op.
}

void ColdTransferCoordinator::shutdown() {
    if (state_ == CoordinatorState::Idle) return;
    correlator_.cancel(active_call_id_);
    state_ = CoordinatorState::Completed;
}

CoordinatorState ColdTransferCoordinator::state() const noexcept {
    return state_;
}

} // namespace voiceai
