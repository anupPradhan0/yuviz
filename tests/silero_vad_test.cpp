#include <gtest/gtest.h>
#include "media/SileroVAD.h"

#include <cmath>
#include <cstdio>
#include <string>
#include <tuple>
#include <vector>

namespace {

using namespace voiceai;

// Tests run from the repo root or from build/ — try both.
std::string find_model() {
    for (const char* p : {"models/silero_vad.onnx", "../models/silero_vad.onnx"}) {
        if (std::FILE* f = std::fopen(p, "rb")) {
            std::fclose(f);
            return p;
        }
    }
    return {};
}

std::vector<int16_t> silence_frame(size_t samples = 320) {
    return std::vector<int16_t>(samples, 0);
}

std::vector<int16_t> tone_frame(float amplitude = 0.8f, size_t samples = 320,
                                float freq_hz = 440.0f) {
    std::vector<int16_t> buf(samples);
    for (size_t i = 0; i < samples; ++i) {
        const float t = static_cast<float>(i) / 16000.0f;
        buf[i] = static_cast<int16_t>(amplitude * 32767.0f *
                                      std::sin(2.0f * 3.14159265f * freq_hz * t));
    }
    return buf;
}

class SileroVADTest : public ::testing::Test {
protected:
    void SetUp() override {
        model_path_ = find_model();
        if (model_path_.empty())
            GTEST_SKIP() << "silero_vad.onnx not found — skipping";
    }

    SileroVAD make_vad() {
        SileroVADConfig cfg;
        cfg.model_path = model_path_;
        return SileroVAD{cfg, logger_};
    }

    std::string model_path_;
    Logger      logger_ = Logger::make_null();
};

TEST_F(SileroVADTest, SilenceProducesNoEvent) {
    auto vad = make_vad();
    auto frame = silence_frame();
    for (int i = 0; i < 100; ++i)
        EXPECT_EQ(vad.process(frame.data(), frame.size()), VADEvent::None);
}

// The reason SileroVAD exists: a loud pure tone (TTS echo, DTMF, line noise)
// trips EnergyVAD immediately but is not speech.
TEST_F(SileroVADTest, LoudToneIsNotSpeech) {
    auto vad = make_vad();
    auto frame = tone_frame(0.8f);
    for (int i = 0; i < 100; ++i)
        EXPECT_EQ(vad.process(frame.data(), frame.size()), VADEvent::None)
            << "tone misclassified as speech at frame " << i;
}

TEST_F(SileroVADTest, EnergyStillReported) {
    auto vad = make_vad();
    auto loud = tone_frame(0.8f);
    std::ignore = vad.process(loud.data(), loud.size());
    EXPECT_GT(vad.last_energy_db(), -35.0f);

    auto quiet = silence_frame();
    std::ignore = vad.process(quiet.data(), quiet.size());
    EXPECT_LT(vad.last_energy_db(), -60.0f);
}

TEST_F(SileroVADTest, ResetClearsState) {
    auto vad = make_vad();
    auto frame = tone_frame(0.5f);
    for (int i = 0; i < 10; ++i)
        std::ignore = vad.process(frame.data(), frame.size());
    vad.reset();
    EXPECT_EQ(vad.speech_duration_ms(), 0u);
    auto quiet = silence_frame();
    EXPECT_EQ(vad.process(quiet.data(), quiet.size()), VADEvent::None);
}

TEST_F(SileroVADTest, EmptyFrameReturnsNone) {
    auto vad = make_vad();
    EXPECT_EQ(vad.process(nullptr, 0), VADEvent::None);
}

} // namespace
