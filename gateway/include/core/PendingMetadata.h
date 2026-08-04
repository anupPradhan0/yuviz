#pragma once

#include <condition_variable>
#include <mutex>
#include <optional>
#include <string>

namespace voiceai {

// Rendezvous point between a new WebSocket connection's raw callbacks (lws
// thread) and config_resolver_pool_'s bounded wait for the mod_audio_fork
// metadata text frame (DID/ANI/direction) that arrives before any audio (see
// Application.cpp's connect handler, and CallMetadata in Config.h for what
// the resolved text is parsed into).
//
// Exactly one of {text frame, connection close} ever completes it — the
// first to arrive wins, guarded by `ready`; a second call to either
// fulfill() is a silent no-op. The consumer's wait_for() always returns
// within its timeout regardless of which producer wins, or times out if
// neither does. Kept alive by the shared_ptr refcount across whichever
// closures reference it (typically: on_text, on_close, and the pool task
// doing the wait).
struct PendingMetadata {
    std::mutex                 mutex;
    std::condition_variable    cv;
    bool                       ready{false};
    std::optional<std::string> text;   // nullopt: closed or timed out before arrival

    // Called from the text-frame handler. First call wins.
    void fulfill_with_text(const std::string& msg) {
        std::lock_guard lock{mutex};
        if (ready) return;
        text  = msg;
        ready = true;
        cv.notify_all();
    }

    // Called from the close handler (or any path signaling "never coming").
    // First call wins.
    void fulfill_with_close() {
        std::lock_guard lock{mutex};
        if (ready) return;
        text  = std::nullopt;
        ready = true;
        cv.notify_all();
    }

    // Blocks the calling thread up to `timeout`. Returns the resolved text
    // (nullopt if closed-before-arrival or timed out) — indistinguishable
    // to the caller, both degrade identically via CallMetadata::parse().
    template <typename Rep, typename Period>
    [[nodiscard]] std::optional<std::string> wait_for(
        std::chrono::duration<Rep, Period> timeout) {
        std::unique_lock lock{mutex};
        cv.wait_for(lock, timeout, [&] { return ready; });
        return text;   // still nullopt if wait_for above timed out (ready never set)
    }
};

} // namespace voiceai
