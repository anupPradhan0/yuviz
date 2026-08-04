#pragma once

#include <string>

namespace voiceai {

// Immutable set of correlation IDs propagated on every log line,
// metric label, and distributed trace span for a single session.
struct ObservabilityContext {
    std::string session_id;           // unique per WebSocket connection
    std::string tenant_id;            // multi-tenant routing key
    std::string trace_id;             // W3C traceparent propagated to all services
    std::string call_id;              // telephony call leg (from SIP headers)
    std::string provider_request_id;  // set per STT/LLM/TTS call, rotated each turn

    [[nodiscard]] bool is_valid() const noexcept {
        return !session_id.empty() && !tenant_id.empty();
    }
};

} // namespace voiceai
