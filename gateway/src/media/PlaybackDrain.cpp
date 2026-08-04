#include "media/PlaybackDrain.h"
#include "common/ThreadUtils.h"

#include <chrono>
#include <stdexcept>

namespace voiceai {

PlaybackDrain::PlaybackDrain(PlaybackQueue&        queue,
                             IWebSocketConnection& connection,
                             Logger&               logger)
    : queue_(queue), connection_(connection), logger_(logger)
{}

PlaybackDrain::~PlaybackDrain() { stop(); }

void PlaybackDrain::start() {
    logger_.debug("PlaybackDrain starting");
    running_.store(true, std::memory_order_relaxed);
    thread_ = std::thread([this] {
        set_thread_name("PlaybackDrain");
        loop();
    });
}

void PlaybackDrain::stop() {
    if (!running_.exchange(false, std::memory_order_acq_rel)) return;
    logger_.debug("PlaybackDrain stopping");
    queue_.stop();
    if (thread_.joinable()) thread_.join();
}

void PlaybackDrain::loop() noexcept {
    using clock = std::chrono::steady_clock;

    // Pace at real-time playout, keeping mod_audio_fork at most ~kLead deep.
    // Audio already sent to FreeSWITCH cannot be recalled, so kLead bounds
    // barge-in stop latency; the rest stays here where cancel can clear it.
    constexpr auto kLead = std::chrono::milliseconds{100};

    auto next_playout = clock::now();

    while (running_.load(std::memory_order_acquire)) {
        try {
            auto frame = queue_.pop(std::chrono::milliseconds{50});
            if (!frame) {
                next_playout = clock::now();  // idle — reset pacing baseline
                continue;
            }

            const auto now = clock::now();
            if (next_playout < now) next_playout = now;
            if (next_playout - now > kLead)
                std::this_thread::sleep_until(next_playout - kLead);

            // Empty payload = end-of-response marker; nothing to send.
            if (!frame->payload.empty() && connection_.is_open())
                connection_.send_binary(frame->payload.data(), frame->payload.size());

            next_playout += std::chrono::microseconds{
                static_cast<int64_t>(frame->duration_ms() * 1000.0)};
        } catch (const std::exception& e) {
            logger_.error("PlaybackDrain: send exception what={}", e.what());
        } catch (...) {
            logger_.error("PlaybackDrain: send unknown exception");
        }
    }
}

} // namespace voiceai
