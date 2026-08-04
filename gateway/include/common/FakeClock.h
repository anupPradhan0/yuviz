#pragma once

#include "common/IClock.h"

namespace voiceai {

// Test clock — starts at epoch; advance() moves time forward deterministically.
// Not thread-safe; use only from a single controlling thread in tests.
class FakeClock final : public IClock {
public:
    explicit FakeClock(TimePoint start = TimePoint{}) : current_(start) {}

    [[nodiscard]] TimePoint now() const noexcept override { return current_; }

    void advance(Duration d)  noexcept { current_ += d; }
    void set(TimePoint tp)    noexcept { current_  = tp; }

private:
    TimePoint current_;
};

} // namespace voiceai
