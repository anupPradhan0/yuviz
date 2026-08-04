#include <gtest/gtest.h>
#include "media/EnergyVAD.h"

#include <cmath>
#include <tuple>
#include <vector>

namespace {

using namespace voiceai;

// ── helpers ───────────────────────────────────────────────────────────────────

// Generate a frame of silence (near-zero amplitude).
std::vector<int16_t> silence_frame(size_t samples = 320) {
    return std::vector<int16_t>(samples, 0);
}

// Generate a frame of a sine wave at given amplitude (0.0–1.0 of full scale).
std::vector<int16_t> tone_frame(float amplitude = 0.8f, size_t samples = 320,
                                 float freq_hz = 440.0f, float sample_rate = 16000.0f) {
    std::vector<int16_t> buf(samples);
    for (size_t i = 0; i < samples; ++i) {
        const float t  = static_cast<float>(i) / sample_rate;
        buf[i] = static_cast<int16_t>(amplitude * 32767.0f *
                                      std::sin(2.0f * 3.14159265f * freq_hz * t));
    }
    return buf;
}

// Discard return value of [[nodiscard]] process() in setup steps.
void feed(voiceai::EnergyVAD& vad, const std::vector<int16_t>& frame) {
    std::ignore = vad.process(frame.data(), frame.size());
}

EnergyVADConfig tight_cfg() {
    EnergyVADConfig cfg;
    cfg.speech_threshold_db  = -35.0f;
    cfg.silence_threshold_db = -40.0f;
    cfg.hold_ms  = 60;   // 3 frames at 20 ms
    cfg.frame_ms = 20;
    cfg.onset_ms = 20;   // 1 frame — immediate SpeechStart (legacy test behavior)
    return cfg;
}

// ── tests ─────────────────────────────────────────────────────────────────────

TEST(EnergyVADTest, SilenceProducesNoEvent) {
    EnergyVAD vad{tight_cfg()};
    auto frame = silence_frame();
    for (int i = 0; i < 10; ++i) {
        EXPECT_EQ(vad.process(frame.data(), frame.size()), VADEvent::None);
    }
}

TEST(EnergyVADTest, SpeechStartFiredOnFirstToneFrame) {
    EnergyVAD vad{tight_cfg()};
    auto loud = tone_frame(0.8f);
    EXPECT_EQ(vad.process(loud.data(), loud.size()), VADEvent::SpeechStart);
}

TEST(EnergyVADTest, SpeechStartFiredOnceUntilEnd) {
    EnergyVAD vad{tight_cfg()};
    auto loud = tone_frame(0.8f);

    EXPECT_EQ(vad.process(loud.data(), loud.size()), VADEvent::SpeechStart);
    for (int i = 0; i < 5; ++i)
        EXPECT_EQ(vad.process(loud.data(), loud.size()), VADEvent::None);
}

TEST(EnergyVADTest, SpeechEndAfterHoldFrames) {
    EnergyVAD vad{tight_cfg()};  // hold_ms=60 → 3 frames

    auto loud    = tone_frame(0.8f);
    auto quiet   = silence_frame();

    feed(vad, loud);  // SpeechStart

    // 2 silent frames — hold not expired yet
    EXPECT_EQ(vad.process(quiet.data(), quiet.size()), VADEvent::None);
    EXPECT_EQ(vad.process(quiet.data(), quiet.size()), VADEvent::None);

    // 3rd silent frame — hold expires → SpeechEnd
    EXPECT_EQ(vad.process(quiet.data(), quiet.size()), VADEvent::SpeechEnd);
}

TEST(EnergyVADTest, SilenceResetsDuringHold) {
    EnergyVAD vad{tight_cfg()};

    auto loud  = tone_frame(0.8f);
    auto quiet = silence_frame();

    feed(vad, loud);   // SpeechStart
    feed(vad, quiet);  // silence 1
    feed(vad, quiet);  // silence 2

    // Speech resumes — hold counter resets
    EXPECT_EQ(vad.process(loud.data(), loud.size()), VADEvent::None);  // still in speech

    // 3 more silent frames now needed to end
    feed(vad, quiet);
    feed(vad, quiet);
    EXPECT_EQ(vad.process(quiet.data(), quiet.size()), VADEvent::SpeechEnd);
}

TEST(EnergyVADTest, SpeechDurationTrackingMs) {
    EnergyVAD vad{tight_cfg()};

    EXPECT_EQ(vad.speech_duration_ms(), 0u);

    auto loud = tone_frame(0.8f);
    feed(vad, loud);                                 // SpeechStart (1 frame)
    EXPECT_EQ(vad.speech_duration_ms(), 20u);

    feed(vad, loud);                                 // None (2 frames)
    EXPECT_EQ(vad.speech_duration_ms(), 40u);
}

TEST(EnergyVADTest, SpeechDurationZeroAfterEnd) {
    EnergyVAD vad{tight_cfg()};
    auto loud  = tone_frame(0.8f);
    auto quiet = silence_frame();

    feed(vad, loud);
    feed(vad, quiet);
    feed(vad, quiet);
    feed(vad, quiet);  // SpeechEnd (hold expired)

    EXPECT_EQ(vad.speech_duration_ms(), 0u);
}

TEST(EnergyVADTest, ResetRestoresInitialState) {
    EnergyVAD vad{tight_cfg()};
    auto loud = tone_frame(0.8f);

    feed(vad, loud);  // SpeechStart
    vad.reset();

    EXPECT_EQ(vad.speech_duration_ms(), 0u);
    // After reset, next loud frame must produce SpeechStart again.
    EXPECT_EQ(vad.process(loud.data(), loud.size()), VADEvent::SpeechStart);
}

TEST(EnergyVADTest, OnsetRequiresSustainedEnergy) {
    auto cfg = tight_cfg();
    cfg.onset_ms = 100;  // 5 frames at 20 ms
    EnergyVAD vad{cfg};

    auto loud  = tone_frame(0.8f);
    auto quiet = silence_frame();

    // 4 loud frames — onset not yet satisfied.
    for (int i = 0; i < 4; ++i)
        EXPECT_EQ(vad.process(loud.data(), loud.size()), VADEvent::None);

    // 5th consecutive loud frame → SpeechStart.
    EXPECT_EQ(vad.process(loud.data(), loud.size()), VADEvent::SpeechStart);
    // Duration credits the onset frames.
    EXPECT_EQ(vad.speech_duration_ms(), 100u);
}

TEST(EnergyVADTest, OnsetResetByQuietFrame) {
    auto cfg = tight_cfg();
    cfg.onset_ms = 60;  // 3 frames
    EnergyVAD vad{cfg};

    auto loud  = tone_frame(0.8f);
    auto quiet = silence_frame();

    // 2 loud frames, then a quiet one — onset counter must reset.
    feed(vad, loud);
    feed(vad, loud);
    feed(vad, quiet);

    // 2 more loud frames still not enough (counter restarted).
    EXPECT_EQ(vad.process(loud.data(), loud.size()), VADEvent::None);
    EXPECT_EQ(vad.process(loud.data(), loud.size()), VADEvent::None);
    // 3rd consecutive → SpeechStart.
    EXPECT_EQ(vad.process(loud.data(), loud.size()), VADEvent::SpeechStart);
}

TEST(EnergyVADTest, EmptyFrameReturnsNone) {
    EnergyVAD vad{tight_cfg()};
    EXPECT_EQ(vad.process(nullptr, 0), VADEvent::None);
}

TEST(EnergyVADTest, LastEnergyDbIsNegativeForSilence) {
    EnergyVAD vad{tight_cfg()};
    auto quiet = silence_frame();
    feed(vad, quiet);
    EXPECT_LT(vad.last_energy_db(), -60.0f);  // silence is very quiet
}

TEST(EnergyVADTest, LastEnergyDbIsAboveThresholdForTone) {
    EnergyVAD vad{tight_cfg()};
    auto loud = tone_frame(0.8f);
    feed(vad, loud);
    EXPECT_GT(vad.last_energy_db(), -35.0f);  // speech threshold
}

} // namespace
