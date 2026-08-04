#pragma once

#include "timer/ITimerService.h"
#include "common/IClock.h"
#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "logging/Logger.h"

#include <atomic>
#include <condition_variable>
#include <mutex>
#include <queue>
#include <thread>
#include <unordered_set>
#include <vector>

namespace voiceai {

// Production timer service: single worker thread, min-heap of scheduled timers.
// Timer callbacks are invoked on the worker thread — must not block.
class TimerService final : public ITimerService, private NonCopyable, private NonMovable {
public:
    TimerService(IClock& clock, Logger& logger);
    ~TimerService() override;

    bool start() override;
    void stop()  override;

    [[nodiscard]] TimerId schedule(TimerType                 type,
                                   const std::string&        session_id,
                                   std::chrono::milliseconds delay,
                                   TimerCallback             callback) override;

    void cancel(TimerId id) override;

private:
    struct Entry {
        IClock::TimePoint deadline;
        TimerId           id;
        TimerType         type;
        std::string       session_id;
        TimerCallback     callback;

        bool operator>(const Entry& o) const noexcept { return deadline > o.deadline; }
    };

    void timer_loop();

    IClock&             clock_;
    Logger&             logger_;
    std::thread         worker_;
    std::atomic<bool>   running_{false};

    mutable std::mutex              mutex_;
    std::condition_variable         cv_;
    // Min-heap: earliest deadline at top.
    std::priority_queue<Entry, std::vector<Entry>, std::greater<Entry>> heap_;
    std::unordered_set<TimerId>     cancelled_;
    std::atomic<TimerId>            next_id_{1};
};

} // namespace voiceai
