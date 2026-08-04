#pragma once

#include <chrono>

namespace voiceai {

// Abstraction over time so business code never calls steady_clock::now() directly.
// Inject IClock everywhere time is observed; use FakeClock in tests for determinism.
class IClock {
public:
    using TimePoint = std::chrono::steady_clock::time_point;
    using Duration  = std::chrono::steady_clock::duration;

    virtual ~IClock() = default;

    [[nodiscard]] virtual TimePoint now() const noexcept = 0;
};

} // namespace voiceai
