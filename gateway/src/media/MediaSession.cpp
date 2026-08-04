#include "media/MediaSession.h"

#include <chrono>
#include <cstdint>

namespace voiceai {

MediaSession::MediaSession(std::string              session_id,
                           uint32_t                 sample_rate,
                           uint8_t                  channels,
                           uint32_t                 ring_buffer_ms,
                           uint32_t                 frame_ms,
                           std::unique_ptr<IVAD>    vad,
                           size_t                   playback_max_frames,
                           IMetrics&                metrics,
                           IClock&                  clock,
                           Logger&                  logger)
    : session_id_   (std::move(session_id))
    , sample_rate_  (sample_rate)
    , frame_samples_(sample_rate * channels * frame_ms / 1000u)
    , metrics_      (metrics)
    , clock_        (clock)
    , logger_       (logger)
    , ring_         ((sample_rate * channels * ring_buffer_ms) / 1000u)
    , vad_          (std::move(vad))
    , playback_queue_(session_id_, playback_max_frames, metrics, logger)
    , frame_buf_    (frame_samples_)
{}

bool MediaSession::push_inbound(const uint8_t* data, size_t byte_len) noexcept {
    const size_t sample_count = byte_len / sizeof(int16_t);
    const bool ok = ring_.push(reinterpret_cast<const int16_t*>(data), sample_count);
    if (!ok) {
        metrics_.increment("media_session.ring_overflow");
        logger_.warn("MediaSession ring overflow session={}", session_id_);
    }
    return ok;
}

size_t MediaSession::drain_available() noexcept {
    const size_t got = ring_.pop(frame_buf_.data(), frame_samples_);
    if (got == 0) return 0;

    metrics_.increment("media_session.frames_drained");

    // Build a frame and fire the audio callback before VAD processing so
    // callers receive every frame regardless of VAD state.
    if (callbacks_.on_audio_frame) {
        AudioFrame f;
        f.set_session_id(session_id_);
        f.sequence_num  = inbound_seq_.fetch_add(1, std::memory_order_relaxed);
        f.timestamp_us  = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                clock_.now().time_since_epoch()).count());
        f.sample_rate   = sample_rate_;
        f.channels      = 1;
        f.direction     = AudioDirection::Inbound;
        f.codec         = AudioCodec::PCM_S16LE;
        f.payload.assign(
            reinterpret_cast<const uint8_t*>(frame_buf_.data()),
            reinterpret_cast<const uint8_t*>(frame_buf_.data()) + got * sizeof(int16_t));
        callbacks_.on_audio_frame(std::move(f));
    }

    if (vad_) {
        const VADEvent ev = vad_->process(frame_buf_.data(), got);

        if (ev == VADEvent::SpeechStart) {
            logger_.debug("MediaSession SpeechStart energy={:.1f}dB session={}",
                          vad_->last_energy_db(), session_id_);
            if (callbacks_.on_speech_started)
                callbacks_.on_speech_started(vad_->last_energy_db());
        } else if (ev == VADEvent::SpeechEnd) {
            const uint32_t dur = vad_->speech_duration_ms();
            logger_.debug("MediaSession SpeechEnd duration={}ms session={}",
                          dur, session_id_);
            if (callbacks_.on_speech_ended)
                callbacks_.on_speech_ended(dur, vad_->last_energy_db());
        }
    }
    return got;
}

void MediaSession::push_outbound(AudioFrame frame) {
    playback_active_.store(true, std::memory_order_relaxed);
    if (frame.is_final)
        final_queued_.store(true, std::memory_order_release);
    playback_queue_.push(std::move(frame));
}

void MediaSession::cancel_playback() {
    playback_active_.store(false, std::memory_order_relaxed);
    final_queued_.store(false, std::memory_order_relaxed);
    playback_queue_.clear();
    if (callbacks_.on_playback_cancelled)
        callbacks_.on_playback_cancelled();
}

void MediaSession::set_callbacks(MediaSessionCallbacks cbs) {
    callbacks_ = std::move(cbs);

    // The queue also empties transiently between sentences; only report
    // playback_finished once the response's final frame has drained.
    playback_queue_.set_on_drained([this] {
        if (!final_queued_.exchange(false, std::memory_order_acq_rel))
            return;
        playback_active_.store(false, std::memory_order_relaxed);
        if (callbacks_.on_playback_finished)
            callbacks_.on_playback_finished();
    });
}

bool MediaSession::has_inbound() const noexcept {
    return ring_.available() >= frame_samples_;
}

} // namespace voiceai
