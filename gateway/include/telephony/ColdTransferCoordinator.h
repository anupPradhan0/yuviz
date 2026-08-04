#pragma once

#include "logging/ContextLogger.h"
#include "telephony/EslClient.h"
#include "telephony/ITransferCoordinator.h"
#include "telephony/TransferCorrelator.h"

#include <atomic>

namespace voiceai {

// uuid_transfer-based cold transfer — moved verbatim out of CallSession's
// former h.on_transfer_requested body (see docs/warm_transfer_architecture.md
// §1/§10: this is a pure extraction, zero behavior change). Redirects the
// caller's own channel into FreeSWITCH's dialplan; the AI's connection to
// that channel ends the moment the command is accepted, so there is no
// leg to recover if the destination never answers — see WarmTransferCoordinator
// for the strategy that fixes this.
class ColdTransferCoordinator final : public ITransferCoordinator {
public:
    ColdTransferCoordinator(EslClient& esl_client, TransferCorrelator& correlator,
                            ContextLogger& log);

    void start(TransferCoordinatorContext ctx, TransferCoordinatorCallbacks callbacks) override;
    void cancel() override;
    void shutdown() override;
    [[nodiscard]] CoordinatorState state() const noexcept override;

private:
    EslClient&           esl_client_;
    TransferCorrelator&  correlator_;
    ContextLogger&        log_;

    // Atomic — see WarmTransferCoordinator::state_'s own comment for why
    // (read cross-thread from ~CallSession()'s bounded wait-loop).
    std::atomic<CoordinatorState> state_{CoordinatorState::Idle};
    std::string                   active_call_id_;
    TransferCoordinatorCallbacks  callbacks_;
};

} // namespace voiceai
