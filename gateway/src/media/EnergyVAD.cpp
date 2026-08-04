#include "media/EnergyVAD.h"

#include <cmath>
#include <cstdint>
#include <limits>

namespace voiceai {

EnergyVAD::EnergyVAD(EnergyVADConfig cfg) noexcept
    : cfg_(cfg)
    , hold_frames_(cfg.frame_ms > 0 ? cfg.hold_ms / cfg.frame_ms : 12u)
    , onset_needed_(cfg.frame_ms > 0 && cfg.onset_ms > 0
                        ? (cfg.onset_ms + cfg.frame_ms - 1) / cfg.frame_ms
                        : 1u)
{}

VADEvent EnergyVAD::process(const int16_t* samples, size_t count) noexcept {
    if (count == 0) return VADEvent::None;

    last_energy_db_ = compute_energy_db(samples, count);

    if (!in_speech_) {
        if (last_energy_db_ >= cfg_.speech_threshold_db) {
            // Require sustained energy: a single loud frame is indistinguishable
            // from an echo blip, and a false SpeechStart kills playback.
            if (++onset_frames_ >= onset_needed_) {
                in_speech_      = true;
                silence_frames_ = 0;
                speech_frames_  = onset_frames_;
                onset_frames_   = 0;
                return VADEvent::SpeechStart;
            }
        } else {
            onset_frames_ = 0;
        }
        return VADEvent::None;
    }

    // Currently in speech.
    if (last_energy_db_ < cfg_.silence_threshold_db) {
        ++silence_frames_;
        if (silence_frames_ >= hold_frames_) {
            in_speech_      = false;
            silence_frames_ = 0;
            speech_frames_  = 0;
            return VADEvent::SpeechEnd;
        }
    } else {
        silence_frames_ = 0;
        ++speech_frames_;
    }
    return VADEvent::None;
}

uint32_t EnergyVAD::speech_duration_ms() const noexcept {
    return in_speech_ ? speech_frames_ * cfg_.frame_ms : 0u;
}

void EnergyVAD::reset() noexcept {
    in_speech_      = false;
    silence_frames_ = 0;
    speech_frames_  = 0;
    onset_frames_   = 0;
    last_energy_db_ = -96.0f;
}

float EnergyVAD::compute_energy_db(const int16_t* samples, size_t count) noexcept {
    if (count == 0) return -96.0f;

    double sum = 0.0;
    for (size_t i = 0; i < count; ++i) {
        const double s = static_cast<double>(samples[i]) / 32768.0;
        sum += s * s;
    }
    const double rms = std::sqrt(sum / static_cast<double>(count));
    if (rms < 1e-10) return -96.0f;
    return static_cast<float>(20.0 * std::log10(rms));
}

} // namespace voiceai
