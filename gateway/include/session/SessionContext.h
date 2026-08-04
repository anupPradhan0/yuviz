#pragma once

#include "config/Config.h"
#include "observability/ObservabilityContext.h"

#include <memory>
#include <string>

namespace voiceai {

// Immutable per-session identity, routing information, and configuration snapshot.
// Bound once at call creation; never mutated during a call.
//
// tenant is a shared_ptr<const TenantConfig> so one allocation can be
// shared across all sessions of the same tenant without copying.
struct SessionContext {
    ObservabilityContext                    obs;     // session_id, tenant_id, trace_id, call_id
    std::shared_ptr<const TenantConfig>    tenant;  // per-session config snapshot (Principle 6)
    std::string                            caller_did;
    std::string                            called_did;
    std::string                            direction{"inbound"};
    std::string                            script_id;   // conversation script / persona for this tenant
};

} // namespace voiceai
