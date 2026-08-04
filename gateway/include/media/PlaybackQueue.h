#pragma once

#include "common/AudioFrame.h"
#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "logging/Logger.h"
#include "metrics/IMetrics.h"

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <functional>
#include <mutex>
#include <optional>
#include <queue>
#include <string>

namespace voiceai {

// Bounded outbound audio queue: TTS chunks arrive here and are consumed by
// the playback thread that sends PCM back to FreeSWITCH.
//
// Overflow policy: drop-newest.  Dropping the oldest frame corrupts speech
// that is about to play; rejecting the newest only truncates the tail of an
// over-long response.  MediaSession tracks the end-of-response marker before
// push(), so playback_finished survives a rejected marker frame.
class PlaybackQueue : private NonCopyable, private NonMovable {
public:
    using DrainCallback = std::function<void()>;  // fired when queue becomes empty

    PlaybackQueue(std::string session_id,
                  size_t      max_frames,
                  IMetrics&   metrics,
                  Logger&     logger);

    // Push a TTS audio frame.  Thread-safe.  Rejects the frame on overflow.
    void push(AudioFrame frame);

    // Pop the next frame.  Blocks up to timeout; returns nullopt on timeout/stop.
    std::optional<AudioFrame> pop(std::chrono::milliseconds timeout = std::chrono::milliseconds{100});

    // Discard all queued frames (barge-in cancellation).
    void clear();

    // Signal the consumer to stop blocking and return.
    void stop();

    void set_on_drained(DrainCallback cb);

    [[nodiscard]] size_t size()     const noexcept;
    [[nodiscard]] bool   is_empty() const noexcept;
    [[nodiscard]] float  fill_pct() const noexcept;

private:
    std::string         session_id_;
    size_t              max_frames_;
    IMetrics&           metrics_;
    Logger&             logger_;

    mutable std::mutex       mutex_;
    std::condition_variable  cv_;
    std::queue<AudioFrame>   queue_;
    bool                     stopped_{false};
    uint64_t                 dropped_{0};   // frames rejected in current overflow episode
    DrainCallback            on_drained_;
};

} // namespace voiceai
