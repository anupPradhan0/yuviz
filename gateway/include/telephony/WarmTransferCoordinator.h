#pragma once

#include "logging/ContextLogger.h"
#include "telephony/EslClient.h"
#include "telephony/ITransferCoordinator.h"
#include "telephony/TransferCorrelator.h"

#include <atomic>

namespace voiceai {

// Bridge-based (attended) transfer — see docs/warm_transfer_architecture.md
// in full; this is the v1 implementation of that design. Unlike
// ColdTransferCoordinator, the caller's own channel (`ctx.call_id`) is
// NEVER redirected: a second, independent leg is originated to the human
// destination, and the two are bridged together only once that leg
// answers. If it never answers, the caller's leg was never touched at
// all — on_transfer_completed(false, ...) fires and the Conversation
// Service's existing apology-and-continue path (TransferFailed) is
// genuinely reachable, unlike cold's.
//
// Sequence (see docs/warm_transfer_architecture.md §4/§5 for the full
// diagrams):
//   start() → hold(customer) → originate_async(destination) → [async]
//   BACKGROUND_JOB(job_uuid) → success: agent leg answered (see the
//   on_job_resolved() doc comment for why BACKGROUND_JOB's own success is
//   treated as the answer signal — a deliberate, flagged deviation from
//   watching a separate CHANNEL_ANSWER) → unhold(customer) →
//   stop_audio_fork(customer) [confirmed] → bridge(customer, agent) →
//   on_transfer_completed(true, ...)
//   failure at any point → uuid_kill(agent leg, if it exists) →
//   unhold(customer) → on_transfer_completed(false, ..., reason)
//
// Caller ID (agent.caller_id_policy) and waiting experience (agent.
// transfer_waiting_experience) are both now per-attempt, carried on
// TransferCoordinatorContext — see that struct's own comment. Caller ID is
// fully resolved by the Conversation Service/CallSession before reaching
// here; waiting experience is a raw config value this coordinator itself
// switches on (see start()). Cancellation is not yet wired here (VAD-
// triggered cancel() call is CallSession's responsibility to invoke, once
// barge-in-during-Hold detection exists on that side).
class WarmTransferCoordinator final : public ITransferCoordinator {
public:
    WarmTransferCoordinator(EslClient& esl_client, TransferCorrelator& leg_correlator,
                            TransferCorrelator& job_correlator, ContextLogger& log);

    void start(TransferCoordinatorContext ctx, TransferCoordinatorCallbacks callbacks) override;
    void cancel() override;
    void shutdown() override;
    [[nodiscard]] CoordinatorState state() const noexcept override;

private:
    void on_job_resolved(bool success, std::string result);
    void finish(bool success, std::string destination, std::string detail);

    EslClient&           esl_client_;
    TransferCorrelator&  leg_correlator_;   // agent-leg uuid keyspace (shared with cold)
    TransferCorrelator&  job_correlator_;   // bgapi Job-UUID keyspace
    ContextLogger&        log_;

    // Atomic: read from ~CallSession()'s bounded wait-loop (running on
    // session_cleanup_pool_'s thread — see that destructor's own comment)
    // while written from here on EslEventListener's thread. Confirmed live
    // (2026-07-20): a plain enum member here produced a genuine data race
    // that crashed the Gateway (SIGSEGV) shortly after a successful
    // transfer, immediately following ~CallSession()'s call into this
    // exact wait-loop.
    std::atomic<CoordinatorState> state_{CoordinatorState::Idle};
    MediaOwnership   media_owner_{MediaOwnership::Gateway};
    std::string      customer_uuid_;   // ctx.call_id — never redirected, only held/unheld
    std::string      job_uuid_;
    std::string      agent_uuid_;      // known only after BACKGROUND_JOB succeeds
    std::string      destination_;
    std::string       transfer_id_;
    std::string       caller_id_;          // ctx.caller_id — see start()
    std::string       waiting_experience_; // ctx.waiting_experience — see start()
    TransferCoordinatorCallbacks callbacks_;
};

} // namespace voiceai
