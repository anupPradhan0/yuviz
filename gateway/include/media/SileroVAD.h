#pragma once

#include "common/NonCopyable.h"
#include "logging/Logger.h"
#include "media/IVAD.h"
#include "media/SileroVADConfig.h"

#include <array>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace voiceai {

// Neural VAD behind IVAD, backed by the Silero v6 ONNX model.
//
// Unlike EnergyVAD it scores speech *probability*, so echo, tones, and noise
// do not trigger SpeechStart.  The same onset/hold hysteresis semantics apply,
// counted in Silero's 32 ms windows.
//
// Feed granularity: process() accepts arbitrary frame sizes (20 ms frames from
// AudioWorkerPool) and internally rebuffers to 512-sample windows with the
// 64-sample context prefix the model expects.  16 kHz mono only.
//
// Threading: one instance per MediaSession, driven by a single AudioWorker
// thread — no internal locking.  Constructor throws if the model cannot be
// loaded; CallSessionFactory falls back to EnergyVAD.
class SileroVAD final : public IVAD, private NonCopyable {
public:
    SileroVAD(SileroVADConfig cfg, Logger& logger);
    ~SileroVAD() override;

    [[nodiscard]] VADEvent process(const int16_t* samples,
                                   size_t          count) noexcept override;

    [[nodiscard]] float    last_energy_db()     const noexcept override { return last_energy_db_; }
    [[nodiscard]] uint32_t speech_duration_ms() const noexcept override;

    void reset() noexcept override;

private:
    static constexpr size_t   kWindow   = 512;  // samples per inference at 16 kHz
    static constexpr size_t   kContext  = 64;   // trailing samples of previous window
    static constexpr uint32_t kWindowMs = 32;

    // Runs inference on the first kWindow samples of accum_ and consumes them.
    [[nodiscard]] float infer_window();

    struct OrtState;  // pimpl — keeps onnxruntime headers out of gateway includes
    std::unique_ptr<OrtState> ort_;

    SileroVADConfig cfg_;
    Logger&         logger_;

    std::vector<float>            accum_;      // pending samples, float [-1,1]
    std::array<float, kContext>   context_{};  // tail of the last processed window
    std::array<float, 128>        h_{};        // LSTM state, carried across windows
    std::array<float, 128>        c_{};

    bool     in_speech_{false};
    uint32_t onset_windows_{0};
    uint32_t silence_windows_{0};
    uint32_t speech_windows_{0};
    uint32_t onset_needed_{3};
    uint32_t hold_needed_{16};
    uint32_t infer_errors_{0};
    float    last_energy_db_{-96.0f};
};

} // namespace voiceai
