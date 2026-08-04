#pragma once

#include <cstddef>
#include <cstdint>

namespace voiceai {

enum class VADEvent : uint8_t {
    None,        // no change
    SpeechStart, // energy crossed above threshold
    SpeechEnd,   // energy dropped below threshold for hold_ms
};

// Voice Activity Detector interface.
// Called on every 20 ms audio frame from AudioWorkerPool — must be non-blocking,
// non-allocating, and never throw.
class IVAD {
public:
    virtual ~IVAD() = default;

    // Feed one frame of L16 PCM samples.  Returns a VADEvent.
    // samples: pointer to int16_t samples (not bytes)
    // count:   number of samples (= frame_ms * sample_rate / 1000)
    [[nodiscard]] virtual VADEvent process(const int16_t* samples,
                                           size_t          count) noexcept = 0;

    // Energy of the most recently processed frame in dBFS.
    [[nodiscard]] virtual float last_energy_db() const noexcept = 0;

    // Duration of the current speech segment in ms (0 if not speaking).
    [[nodiscard]] virtual uint32_t speech_duration_ms() const noexcept = 0;

    virtual void reset() noexcept = 0;
};

} // namespace voiceai
