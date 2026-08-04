#pragma once

#include "common/IClock.h"
#include <chrono>

namespace voiceai {

// Production clock — delegates to std::chrono::steady_clock.
class SystemClock final : public IClock {
public:
    [[nodiscard]] TimePoint now() const noexcept override {
        return std::chrono::steady_clock::now();
    }
};

} // namespace voiceai
