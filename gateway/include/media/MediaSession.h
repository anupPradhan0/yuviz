#pragma once

#include "common/AudioFrame.h"
#include "common/IClock.h"
#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "logging/Logger.h"
#include "media/IVAD.h"
#include "media/PlaybackQueue.h"
#include "metrics/IMetrics.h"
#include "utils/RingBuffer.h"

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>

namespace voiceai {

// Callbacks fired by MediaSession (invoked on AudioWorker thread unless noted).
struct MediaSessionCallbacks {
    // Fired for every drained PCM frame — used by CallSession to forward audio
    // to the ConversationService via the control queue.
    std::function<void(AudioFrame)>                   on_audio_frame;

    std::function<void(float energy_db)>              on_speech_started;
    std::function<void(uint32_t duration_ms,
                       float energy_db)>              on_speech_ended;
    std::function<void()>                             on_playback_finished;
    std::function<void()>                             on_playback_cancelled;
};

// Data-plane object owned by one CallSession.
// AudioWorkerPool holds a raw pointer and calls drain_available() on a fixed thread.
// The IWebSocketServer lws thread calls push_inbound() from its receive callback.
//
// SPSC invariant: push_inbound is the single producer;
//                 drain_available (via AudioWorkerPool) is the single consumer.
class MediaSession : private NonCopyable, private NonMovable {
public:
    MediaSession(std::string              session_id,
                 uint32_t                 sample_rate,
                 uint8_t                  channels,
                 uint32_t                 ring_buffer_ms,
                 uint32_t                 frame_ms,
                 std::unique_ptr<IVAD>    vad,
                 size_t                   playback_max_frames,
                 IMetrics&                metrics,
                 IClock&                  clock,
                 Logger&                  logger);

    ~MediaSession() = default;

    // Producer side: called from lws WebSocket receive callback.
    // Thread: lws service thread.
    bool push_inbound(const uint8_t* data, size_t byte_len) noexcept;

    // Consumer side: called by AudioWorkerPool on assigned worker thread.
    // Drains one frame, runs VAD, fires callbacks.  Returns number of samples consumed.
    size_t drain_available() noexcept;

    // Push a TTS chunk into the PlaybackQueue (outbound).
    // Thread-safe; called from gRPC receive thread.
    void push_outbound(AudioFrame frame);

    // Cancel in-flight playback (barge-in).
    void cancel_playback();

    // ORDERING CONSTRAINT (TS-4): set_callbacks() MUST be called before
    // AudioWorkerPool::assign().  The two-phase ordering is:
    //   1. set_callbacks()  — writes callbacks_ on the constructing thread
    //   2. assign()         — acquires sessions_mutex (release), making the
    //                         write visible to the worker thread which acquires
    //                         sessions_mutex (acquire) on every snapshot.
    // Calling set_callbacks() after assign() races with drain_available().
    void set_callbacks(MediaSessionCallbacks cbs);

    [[nodiscard]] const std::string& session_id()    const noexcept { return session_id_; }
    [[nodiscard]] bool               has_inbound()   const noexcept;
    [[nodiscard]] bool               is_playing()    const noexcept { return playback_active_.load(std::memory_order_relaxed); }
    [[nodiscard]] PlaybackQueue&     playback_queue() noexcept { return playback_queue_; }

private:
    std::string           session_id_;
    uint32_t              sample_rate_;
    uint32_t              frame_samples_;
    IMetrics&             metrics_;
    IClock&               clock_;
    Logger&               logger_;

    RingBuffer<int16_t>   ring_;
    std::unique_ptr<IVAD> vad_;
    PlaybackQueue         playback_queue_;

    MediaSessionCallbacks callbacks_;        // written once (set_callbacks) before T2/T4 start

    std::vector<int16_t>  frame_buf_;       // only on AudioWorker thread (T2)
    std::atomic<uint64_t> inbound_seq_{0};  // monotonic per-session frame counter

    // Written by gRPC reader thread (T3) in push_outbound() and by the playback
    // consumer thread (T4) inside PlaybackQueue::on_drained.  Must be atomic.
    std::atomic<bool>     playback_active_{false};

    // Set when the response's final frame is queued; gates on_drained so
    // transient mid-response queue gaps aren't reported as end of playback.
    std::atomic<bool>     final_queued_{false};
};

} // namespace voiceai
