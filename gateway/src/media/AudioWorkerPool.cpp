#include "media/AudioWorkerPool.h"
#include "common/ThreadUtils.h"

#include <algorithm>
#include <chrono>
#include <stdexcept>

namespace voiceai {

AudioWorkerPool::AudioWorkerPool(size_t worker_count, uint32_t frame_ms, Logger& logger)
    : worker_count_(worker_count > 0 ? worker_count : 1)
    , frame_ms_    (frame_ms > 0 ? frame_ms : 20u)
    , logger_      (logger)
{}

AudioWorkerPool::~AudioWorkerPool() {
    stop();
}

bool AudioWorkerPool::start() {
    if (running_.exchange(true)) return true;

    // Every Worker must be in workers_ before any thread starts — worker_loop()
    // reads workers_[idx] immediately, and a thread launched mid-loop can run
    // ahead of the push_back() that makes its own slot visible (caught by ASan
    // as a race on the not-yet-inserted vector element).
    workers_.reserve(worker_count_);
    for (size_t i = 0; i < worker_count_; ++i) {
        workers_.push_back(std::make_unique<Worker>());
    }
    for (size_t i = 0; i < worker_count_; ++i) {
        workers_[i]->thread = std::thread([this, i] { worker_loop(i); });
    }
    logger_.info("AudioWorkerPool started workers={}", worker_count_);
    return true;
}

void AudioWorkerPool::stop() {
    if (!running_.exchange(false)) return;
    for (auto& w : workers_)
        if (w->thread.joinable()) w->thread.join();
    workers_.clear();
    logger_.info("AudioWorkerPool stopped");
}

size_t AudioWorkerPool::assign(MediaSession* session) {
    const size_t idx = round_robin_.fetch_add(1, std::memory_order_relaxed) % worker_count_;

    auto entry    = std::make_shared<SessionEntry>();
    entry->session = session;

    {
        std::lock_guard lock{workers_[idx]->sessions_mutex};
        workers_[idx]->sessions.push_back(std::move(entry));
    }
    session_count_.fetch_add(1, std::memory_order_relaxed);
    logger_.debug("AudioWorkerPool: assigned session={} to worker={}",
                  session->session_id(), idx);
    return idx;
}

void AudioWorkerPool::unassign(MediaSession* session) {
    std::shared_ptr<SessionEntry> entry;

    for (auto& w : workers_) {
        std::lock_guard lock{w->sessions_mutex};
        auto it = std::find_if(w->sessions.begin(), w->sessions.end(),
                               [&](const auto& e) { return e->session == session; });
        if (it != w->sessions.end()) {
            entry = *it;
            // Mark removed while holding sessions_mutex so the worker's
            // double-check (under drain_mutex) sees it immediately.
            entry->removed.store(true, std::memory_order_release);
            w->sessions.erase(it);
            break;
        }
    }

    if (!entry) return;

    session_count_.fetch_sub(1, std::memory_order_relaxed);

    // Block until any in-progress drain_available() for this session finishes.
    // The worker holds drain_mutex while draining; acquiring it here is the barrier.
    // After this lock_guard destructs, no worker thread can ever call
    // drain_available() on this session again (removed=true + entry erased).
    std::lock_guard drain_barrier{entry->drain_mutex};
}

void AudioWorkerPool::worker_loop(size_t idx) {
    set_thread_name("AudioWorker-" + std::to_string(idx));

    while (running_.load(std::memory_order_acquire)) {
        bool did_work = false;

        // Snapshot shared_ptrs so unassign() can safely erase from the vector
        // while we iterate.  Each shared_ptr keeps the SessionEntry alive even
        // after unassign() removes it from the vector.
        std::vector<std::shared_ptr<SessionEntry>> snapshot;
        {
            std::lock_guard lock{workers_[idx]->sessions_mutex};
            snapshot = workers_[idx]->sessions;
        }

        for (auto& entry : snapshot) {
            // Fast pre-check without the drain lock.
            if (entry->removed.load(std::memory_order_acquire))
                continue;
            if (!entry->session->has_inbound())
                continue;

            // Acquire drain_mutex before calling drain_available() so that
            // unassign() is forced to wait if we are mid-drain.
            std::lock_guard drain_lock{entry->drain_mutex};

            // Double-check: unassign() may have set removed=true between the
            // pre-check above and acquiring drain_mutex.
            if (entry->removed.load(std::memory_order_acquire))
                continue;

            try {
                entry->session->drain_available();
            } catch (const std::exception& e) {
                logger_.error("AudioWorkerPool: drain exception session={} what={}",
                              entry->session->session_id(), e.what());
            } catch (...) {
                logger_.error("AudioWorkerPool: drain unknown exception session={}",
                              entry->session->session_id());
            }
            did_work = true;
        }

        if (!did_work)
            std::this_thread::sleep_for(
                std::chrono::microseconds(frame_ms_ * 1000u / 40u));
    }
}

} // namespace voiceai
