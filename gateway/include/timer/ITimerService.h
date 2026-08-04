#pragma once

#include "timer/TimerType.h"

#include <chrono>
#include <cstdint>
#include <functional>
#include <string>

namespace voiceai {

using TimerId = uint64_t;
static constexpr TimerId kInvalidTimer = 0;

// Callback fired on the TimerService worker thread when a timer expires.
using TimerCallback = std::function<void(TimerId, TimerType, const std::string& session_id)>;

class ITimerService {
public:
    virtual ~ITimerService() = default;

    // Schedule a one-shot timer.  Returns a TimerId that can be passed to cancel().
    [[nodiscard]] virtual TimerId schedule(TimerType                    type,
                                           const std::string&           session_id,
                                           std::chrono::milliseconds    delay,
                                           TimerCallback                callback) = 0;

    // Cancel a pending timer.  No-op if the ID is invalid or already fired.
    virtual void cancel(TimerId id) = 0;

    virtual bool start() = 0;
    virtual void stop()  = 0;
};

} // namespace voiceai
