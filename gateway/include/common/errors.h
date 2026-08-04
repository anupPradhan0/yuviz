#pragma once

#include <stdexcept>
#include <string>

namespace voiceai {

// Base error for all gateway faults.
class GatewayError : public std::runtime_error {
public:
    explicit GatewayError(const std::string& msg) : std::runtime_error(msg) {}
};

// Errors originating from the media pipeline (AudioIngress / AudioEgress / etc.).
class MediaError : public GatewayError {
public:
    explicit MediaError(const std::string& msg) : GatewayError(msg) {}
};

// Errors originating from the outbound transport layer.
class TransportError : public GatewayError {
public:
    explicit TransportError(const std::string& msg) : GatewayError(msg) {}
};

// Errors originating from session lifecycle management.
class SessionError : public GatewayError {
public:
    explicit SessionError(const std::string& msg) : GatewayError(msg) {}
};

} // namespace voiceai
