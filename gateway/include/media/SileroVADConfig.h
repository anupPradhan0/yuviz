#pragma once

#include <cstdint>
#include <string>

namespace voiceai {

struct SileroVADConfig {
    std::string model_path{"models/silero_vad.onnx"};
    float    speech_threshold{0.5f};   // window probability >= this → speech
    float    silence_threshold{0.35f}; // window probability <  this → silence
    uint32_t onset_ms{96};             // sustained speech before SpeechStart
    uint32_t hold_ms{1000};            // sustained silence before SpeechEnd.
                                       // Silero drops below silence_threshold the
                                       // instant speech pauses, and natural
                                       // mid-sentence pauses run 0.8-1.2 s —
                                       // 500/700 ms both split utterances in live
                                       // testing.
};

} // namespace voiceai
