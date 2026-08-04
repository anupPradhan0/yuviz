#pragma once

#include <cstdint>

namespace voiceai {

struct EnergyVADConfig {
    float    speech_threshold_db{-35.0f};   // above this → speech
    float    silence_threshold_db{-40.0f};  // below this for hold_ms → speech end
    uint32_t hold_ms{500};                  // silence must persist this long to end speech
    uint32_t frame_ms{20};                  // frame size; used to convert hold_ms to frames
    uint32_t onset_ms{100};                 // energy must persist this long to start speech
};

} // namespace voiceai
