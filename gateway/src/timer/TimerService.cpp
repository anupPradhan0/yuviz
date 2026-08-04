#include "timer/TimerService.h"
#include "common/ThreadUtils.h"

namespace voiceai {

TimerService::TimerService(IClock& clock, Logger& logger)
    : clock_(clock)
    , logger_(logger)
{}

TimerService::~TimerService() {
    stop();
}

bool TimerService::start() {
    if (running_.exchange(true)) return true;
    worker_ = std::thread([this] { timer_loop(); });
    logger_.info("TimerService started");
    return true;
}

void TimerService::stop() {
    if (!running_.exchange(false)) return;
    cv_.notify_all();
    if (worker_.joinable()) worker_.join();
    logger_.info("TimerService stopped");
}

TimerId TimerService::schedule(TimerType                 type,
                                const std::string&        session_id,
                                std::chrono::milliseconds delay,
                                TimerCallback             callback)
{
    const TimerId id = next_id_.fetch_add(1, std::memory_order_relaxed);
    const auto deadline = clock_.now() + delay;

    {
        std::lock_guard lock{mutex_};
        heap_.push({deadline, id, type, session_id, std::move(callback)});
    }
    cv_.notify_one();
    return id;
}

void TimerService::cancel(TimerId id) {
    if (id == kInvalidTimer) return;
    std::lock_guard lock{mutex_};
    cancelled_.insert(id);
    // cancelled_ is bounded: timer_loop() calls cancelled_.erase(id) when the
    // timer fires, so each entry lives at most max_timer_duration (≤30 s).
    // At 1000 sessions × 1 active timer each, peak size is ~1000 entries.
    if (cancelled_.size() > 10'000) [[unlikely]]
        logger_.warn("TimerService: cancelled_ has {} entries — check for timer leaks",
                     cancelled_.size());
}

void TimerService::timer_loop() {
    set_thread_name("TimerSvc");

    while (running_.load(std::memory_order_acquire)) {
        std::unique_lock lock{mutex_};

        if (heap_.empty()) {
            cv_.wait(lock, [this] {
                return !running_.load(std::memory_order_relaxed) || !heap_.empty();
            });
            if (!running_.load(std::memory_order_relaxed)) return;
        }

        // Copy the deadline before releasing the lock.  wait_until() drops the
        // mutex while sleeping; a concurrent schedule() call can then push to
        // heap_ and trigger a vector reallocation, invalidating any const& into
        // heap_ taken before the lock drop — a classic UAF under ASan/TSAN.
        const auto deadline = heap_.top().deadline;
        const auto now      = clock_.now();

        if (deadline > now) {
            cv_.wait_until(lock, deadline);
            continue;
        }

        Entry entry = heap_.top();
        heap_.pop();

        const bool is_cancelled = cancelled_.erase(entry.id) > 0;
        lock.unlock();

        if (!is_cancelled) {
            logger_.trace("TimerService fired id={} type={} session={}",
                          entry.id, to_string(entry.type), entry.session_id);
            entry.callback(entry.id, entry.type, entry.session_id);
        }
    }
}

} // namespace voiceai
