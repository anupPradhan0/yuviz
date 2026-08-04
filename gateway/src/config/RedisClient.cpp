#include "config/RedisClient.h"

#include <hiredis/hiredis.h>

namespace voiceai {

RedisClient::RedisClient(RedisConfig cfg, Logger& logger)
    : cfg_(std::move(cfg))
    , logger_(logger)
{}

RedisClient::~RedisClient() {
    std::lock_guard lock{mutex_};
    disconnect_locked();
}

void RedisClient::disconnect_locked() {
    if (ctx_ != nullptr) {
        ::redisFree(ctx_);
        ctx_ = nullptr;
    }
}

bool RedisClient::ensure_connected_locked() {
    if (ctx_ != nullptr) return true;

    const timeval connect_tv{
        static_cast<time_t>(cfg_.connect_timeout_ms / 1000),
        static_cast<suseconds_t>((cfg_.connect_timeout_ms % 1000) * 1000),
    };
    redisContext* ctx = ::redisConnectWithTimeout(cfg_.host.c_str(), cfg_.port, connect_tv);
    if (ctx == nullptr) {
        logger_.warn("RedisClient: redisConnectWithTimeout returned null host={} port={}",
                     cfg_.host, cfg_.port);
        return false;
    }
    if (ctx->err != 0) {
        logger_.warn("RedisClient: connect failed host={} port={} err={}",
                     cfg_.host, cfg_.port, ctx->errstr);
        ::redisFree(ctx);
        return false;
    }

    const timeval command_tv{
        static_cast<time_t>(cfg_.command_timeout_ms / 1000),
        static_cast<suseconds_t>((cfg_.command_timeout_ms % 1000) * 1000),
    };
    ::redisSetTimeout(ctx, command_tv);

    ctx_ = ctx;
    logger_.info("RedisClient: connected host={} port={}", cfg_.host, cfg_.port);
    return true;
}

std::optional<std::string> RedisClient::get(const std::string& key) {
    if (!cfg_.enabled) return std::nullopt;

    std::lock_guard lock{mutex_};
    if (!ensure_connected_locked()) return std::nullopt;

    redisReply* reply = static_cast<redisReply*>(
        ::redisCommand(ctx_, "GET %s", key.c_str()));

    if (reply == nullptr) {
        // hiredis convention: a null reply means the connection died mid-command.
        logger_.warn("RedisClient: GET failed key={} (connection lost, will reconnect)", key);
        disconnect_locked();
        return std::nullopt;
    }

    std::optional<std::string> result;
    if (reply->type == REDIS_REPLY_STRING) {
        result = std::string(reply->str, static_cast<size_t>(reply->len));
    } else if (reply->type == REDIS_REPLY_NIL) {
        result = std::nullopt;   // cache miss — not an error
    } else if (reply->type == REDIS_REPLY_ERROR) {
        logger_.warn("RedisClient: GET error key={} err={}", key, reply->str);
    }

    ::freeReplyObject(reply);
    return result;
}

} // namespace voiceai
