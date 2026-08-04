#include "media/SileroVAD.h"

#include <onnxruntime_cxx_api.h>

#include <algorithm>
#include <cmath>

namespace voiceai {

struct SileroVAD::OrtState {
    Ort::Env                      env{ORT_LOGGING_LEVEL_ERROR, "silero_vad"};
    Ort::SessionOptions           opts;
    std::unique_ptr<Ort::Session> session;
    Ort::MemoryInfo               mem{Ort::MemoryInfo::CreateCpu(OrtArenaAllocator,
                                                                 OrtMemTypeDefault)};
    std::vector<std::string>      in_names;
    std::vector<std::string>      out_names;
};

SileroVAD::SileroVAD(SileroVADConfig cfg, Logger& logger)
    : ort_(std::make_unique<OrtState>())
    , cfg_(std::move(cfg))
    , logger_(logger)
{
    ort_->opts.SetIntraOpNumThreads(1);
    ort_->opts.SetInterOpNumThreads(1);
    ort_->session = std::make_unique<Ort::Session>(
        ort_->env, cfg_.model_path.c_str(), ort_->opts);

    Ort::AllocatorWithDefaultOptions alloc;
    for (size_t i = 0; i < ort_->session->GetInputCount(); ++i)
        ort_->in_names.emplace_back(
            ort_->session->GetInputNameAllocated(i, alloc).get());
    for (size_t i = 0; i < ort_->session->GetOutputCount(); ++i)
        ort_->out_names.emplace_back(
            ort_->session->GetOutputNameAllocated(i, alloc).get());

    onset_needed_ = std::max(1u, (cfg_.onset_ms + kWindowMs - 1) / kWindowMs);
    hold_needed_  = std::max(1u, (cfg_.hold_ms  + kWindowMs - 1) / kWindowMs);
    accum_.reserve(kWindow * 4);

    logger_.info("SileroVAD loaded model={} onset_windows={} hold_windows={}",
                 cfg_.model_path, onset_needed_, hold_needed_);
}

SileroVAD::~SileroVAD() = default;

VADEvent SileroVAD::process(const int16_t* samples, size_t count) noexcept {
    if (count == 0) return VADEvent::None;

    double sum = 0.0;
    for (size_t i = 0; i < count; ++i) {
        const double s = static_cast<double>(samples[i]) / 32768.0;
        sum += s * s;
        accum_.push_back(static_cast<float>(s));
    }
    const double rms = std::sqrt(sum / static_cast<double>(count));
    last_energy_db_ = rms < 1e-10 ? -96.0f
                                  : static_cast<float>(20.0 * std::log10(rms));

    if (accum_.size() < kWindow) return VADEvent::None;

    float prob = 0.0f;
    try {
        prob = infer_window();
    } catch (const std::exception& e) {
        // Consume the window anyway so accum_ cannot grow without bound.
        accum_.erase(accum_.begin(),
                     accum_.begin() + static_cast<std::ptrdiff_t>(kWindow));
        if (infer_errors_++ % 100 == 0)
            logger_.error("SileroVAD inference failed ({}): {}",
                          infer_errors_, e.what());
        return VADEvent::None;
    }

    if (!in_speech_) {
        if (prob >= cfg_.speech_threshold) {
            if (++onset_windows_ >= onset_needed_) {
                in_speech_      = true;
                speech_windows_ = onset_windows_;
                onset_windows_  = 0;
                silence_windows_ = 0;
                return VADEvent::SpeechStart;
            }
        } else {
            onset_windows_ = 0;
        }
        return VADEvent::None;
    }

    ++speech_windows_;
    if (prob < cfg_.silence_threshold) {
        if (++silence_windows_ >= hold_needed_) {
            in_speech_       = false;
            silence_windows_ = 0;
            speech_windows_  = 0;
            return VADEvent::SpeechEnd;
        }
    } else {
        silence_windows_ = 0;
    }
    return VADEvent::None;
}

float SileroVAD::infer_window() {
    // Model input: [1, 64 context + 512 samples].
    std::array<float, kContext + kWindow> input;
    std::copy(context_.begin(), context_.end(), input.begin());
    std::copy(accum_.begin(),
              accum_.begin() + static_cast<std::ptrdiff_t>(kWindow),
              input.begin() + kContext);

    const int64_t in_shape[2] = {1, static_cast<int64_t>(kContext + kWindow)};
    const int64_t st_shape[3] = {1, 1, 128};

    std::array<Ort::Value, 3> inputs{
        Ort::Value::CreateTensor<float>(ort_->mem, input.data(), input.size(),
                                        in_shape, 2),
        Ort::Value::CreateTensor<float>(ort_->mem, h_.data(), h_.size(),
                                        st_shape, 3),
        Ort::Value::CreateTensor<float>(ort_->mem, c_.data(), c_.size(),
                                        st_shape, 3),
    };

    std::vector<const char*> in_names, out_names;
    for (const auto& n : ort_->in_names)  in_names.push_back(n.c_str());
    for (const auto& n : ort_->out_names) out_names.push_back(n.c_str());

    auto outputs = ort_->session->Run(Ort::RunOptions{nullptr},
                                      in_names.data(), inputs.data(), inputs.size(),
                                      out_names.data(), out_names.size());

    const float prob = outputs[0].GetTensorData<float>()[0];
    std::copy_n(outputs[1].GetTensorData<float>(), h_.size(), h_.begin());
    std::copy_n(outputs[2].GetTensorData<float>(), c_.size(), c_.begin());

    // Context for the next window = tail of this one.
    std::copy(accum_.begin() + static_cast<std::ptrdiff_t>(kWindow - kContext),
              accum_.begin() + static_cast<std::ptrdiff_t>(kWindow),
              context_.begin());
    accum_.erase(accum_.begin(),
                 accum_.begin() + static_cast<std::ptrdiff_t>(kWindow));
    return prob;
}

uint32_t SileroVAD::speech_duration_ms() const noexcept {
    return in_speech_ ? speech_windows_ * kWindowMs : 0u;
}

void SileroVAD::reset() noexcept {
    in_speech_       = false;
    onset_windows_   = 0;
    silence_windows_ = 0;
    speech_windows_  = 0;
    last_energy_db_  = -96.0f;
    accum_.clear();
    context_.fill(0.0f);
    h_.fill(0.0f);
    c_.fill(0.0f);
}

} // namespace voiceai
