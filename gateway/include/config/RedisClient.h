#pragma once

#include "config/Config.h"
#include "logging/Logger.h"

#include <mutex>
#include <optional>
#include <string>

// Forward-declare hiredis's C struct so this header stays hiredis-free for
// everything that just calls get() — only RedisClient.cpp includes <hiredis/hiredis.h>.
struct redisContext;

namespace voiceai {

// Minimal synchronous Redis GET client, used once per new WebSocket
// connection to resolve TenantConfig (see TenantConfig::from_redis) — not on
// the per-audio-frame hot path, so a blocking hiredis call here is fine.
//
// One persistent connection, serialized by a mutex (same shape as
// EslClient): degrades gracefully on any failure — get() returns
// std::nullopt rather than throwing, so a config-plane outage never crashes
// or rejects a call. Lazily reconnects on the next call after a failure.
class RedisClient {
public:
    RedisClient(RedisConfig cfg, Logger& logger);
    ~RedisClient();

    RedisClient(const RedisClient&)            = delete;
    RedisClient& operator=(const RedisClient&) = delete;

    // Returns the string value at `key`, or std::nullopt if the key doesn't
    // exist, Redis is unreachable, or cfg.enabled is false.
    [[nodiscard]] std::optional<std::string> get(const std::string& key);

private:
    bool ensure_connected_locked();
    void disconnect_locked();

    RedisConfig cfg_;
    Logger&     logger_;

    std::mutex     mutex_;
    redisContext*  ctx_{nullptr};
};

} // namespace voiceai
