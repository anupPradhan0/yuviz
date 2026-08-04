#pragma once

#include "timer/ITimerService.h"

#include <chrono>
#include <mutex>
#include <unordered_map>
#include <vector>

namespace voiceai {

// Deterministic timer service for tests.
// Timers never fire on their own — call advance() or fire() explicitly.
class FakeTimerService final : public ITimerService {
public:
    struct PendingTimer {
        TimerId                   id;
        TimerType                 type;
        std::string               session_id;
        std::chrono::milliseconds delay;
        TimerCallback             callback;
    };

    bool start() override { return true; }
    void stop()  override {}

    TimerId schedule(TimerType                 type,
                     const std::string&        session_id,
                     std::chrono::milliseconds delay,
                     TimerCallback             callback) override
    {
        std::lock_guard lock{mutex_};
        const TimerId id = ++next_id_;
        pending_.push_back({id, type, session_id, delay, std::move(callback)});
        return id;
    }

    void cancel(TimerId id) override {
        std::lock_guard lock{mutex_};
        auto it = std::find_if(pending_.begin(), pending_.end(),
            [id](const PendingTimer& t) { return t.id == id; });
        if (it != pending_.end()) pending_.erase(it);
    }

    // Fire all timers whose delay ≤ elapsed.
    void advance(std::chrono::milliseconds elapsed) {
        std::vector<PendingTimer> fired;
        {
            std::lock_guard lock{mutex_};
            auto it = pending_.begin();
            while (it != pending_.end()) {
                if (it->delay <= elapsed) {
                    fired.push_back(std::move(*it));
                    it = pending_.erase(it);
                } else {
                    it->delay -= elapsed;
                    ++it;
                }
            }
        }
        for (auto& t : fired) t.callback(t.id, t.type, t.session_id);
    }

    // Fire a specific timer by ID immediately.
    void fire(TimerId id) {
        PendingTimer t;
        {
            std::lock_guard lock{mutex_};
            auto it = std::find_if(pending_.begin(), pending_.end(),
                [id](const PendingTimer& p) { return p.id == id; });
            if (it == pending_.end()) return;
            t = std::move(*it);
            pending_.erase(it);
        }
        t.callback(t.id, t.type, t.session_id);
    }

    [[nodiscard]] size_t pending_count() const {
        std::lock_guard lock{mutex_};
        return pending_.size();
    }

    [[nodiscard]] bool has_timer(TimerType type) const {
        std::lock_guard lock{mutex_};
        for (const auto& t : pending_)
            if (t.type == type) return true;
        return false;
    }

private:
    mutable std::mutex       mutex_;
    std::vector<PendingTimer> pending_;
    TimerId                  next_id_{0};
};

} // namespace voiceai
