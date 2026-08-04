#include "media/PlaybackQueue.h"

#include <chrono>

namespace voiceai {

PlaybackQueue::PlaybackQueue(std::string session_id,
                             size_t      max_frames,
                             IMetrics&   metrics,
                             Logger&     logger)
    : session_id_(std::move(session_id))
    , max_frames_(max_frames)
    , metrics_(metrics)
    , logger_(logger)
{}

void PlaybackQueue::push(AudioFrame frame) {
    {
        std::lock_guard lock{mutex_};
        if (stopped_) return;

        if (queue_.size() >= max_frames_) {
            // Drop-newest (see header).  Warn every 250 rejects to keep the
            // log quiet during a sustained overflow.
            metrics_.increment("playback_queue.dropped");
            if (dropped_++ % 250 == 0) {
                logger_.warn(
                    "PlaybackQueue overflow — rejecting newest frame "
                    "(dropped={} this episode) session={}",
                    dropped_, session_id_);
            }
            return;
        }
        // While saturated, accepts and rejects alternate — only end the
        // episode once the queue has meaningfully drained.
        if (dropped_ != 0 && queue_.size() < max_frames_ / 2)
            dropped_ = 0;
        queue_.push(std::move(frame));
        metrics_.gauge("playback_queue.size",
                       static_cast<double>(queue_.size()));
    }
    cv_.notify_one();
}

std::optional<AudioFrame> PlaybackQueue::pop(std::chrono::milliseconds timeout) {
    std::unique_lock lock{mutex_};
    const auto deadline = std::chrono::steady_clock::now() + timeout;

    cv_.wait_until(lock, deadline,
        [this] { return !queue_.empty() || stopped_; });

    if (queue_.empty()) return std::nullopt;

    AudioFrame frame = std::move(queue_.front());
    queue_.pop();

    const bool now_empty = queue_.empty();
    metrics_.gauge("playback_queue.size", static_cast<double>(queue_.size()));

    DrainCallback cb;
    if (now_empty && on_drained_) cb = on_drained_;
    lock.unlock();

    if (cb) cb();
    return frame;
}

void PlaybackQueue::clear() {
    std::lock_guard lock{mutex_};
    while (!queue_.empty()) queue_.pop();
    metrics_.gauge("playback_queue.size", 0.0);
    logger_.debug("PlaybackQueue cleared session={}", session_id_);
}

void PlaybackQueue::stop() {
    {
        std::lock_guard lock{mutex_};
        stopped_ = true;
    }
    cv_.notify_all();
}

void PlaybackQueue::set_on_drained(DrainCallback cb) {
    std::lock_guard lock{mutex_};
    on_drained_ = std::move(cb);
}

size_t PlaybackQueue::size() const noexcept {
    std::lock_guard lock{mutex_};
    return queue_.size();
}

bool PlaybackQueue::is_empty() const noexcept {
    std::lock_guard lock{mutex_};
    return queue_.empty();
}

float PlaybackQueue::fill_pct() const noexcept {
    if (max_frames_ == 0) return 0.0f;
    std::lock_guard lock{mutex_};
    return static_cast<float>(queue_.size()) / static_cast<float>(max_frames_) * 100.0f;
}

} // namespace voiceai
