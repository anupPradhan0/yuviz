#pragma once

#include <string>

namespace voiceai {

// Bundles what EslClient::transfer() (and the CallSession/CallFSM plumbing
// around it) needs for one cold transfer attempt — replaces the
// uuid/destination/reason parameter list that would otherwise keep growing
// as this feature does (see the AI-to-human transfer architecture review in
// project memory). transfer_type is carried through for logging/future use
// (e.g. a future warm-transfer branch) — Phase 5's EslClient::transfer()
// itself is cold-only and does not yet branch on it.
struct TransferRequest {
    std::string call_id;        // FreeSWITCH channel UUID being transferred
    std::string transfer_type;  // "warm" | "cold" | "none" — see TransferType (services/conversation/directives.py)
    std::string destination;    // phone number/extension, or a sip:/sips: URI
    std::string reason;
    std::string transfer_id{};  // observability-only correlation id (may be empty)
};

} // namespace voiceai
