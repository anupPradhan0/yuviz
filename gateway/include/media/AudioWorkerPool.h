#pragma once

#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "logging/Logger.h"
#include "media/MediaSession.h"

#include <atomic>
#include <cstddef>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

namespace voiceai {

// Fixed pool of M worker threads that drain MediaSession ring buffers.
//
// SPSC invariant: each MediaSession is assigned to exactly one worker at
// creation time (round-robin) and never reassigned.
//
// Teardown safety: unassign() synchronises — after it returns, no worker
// thread is executing or will execute drain_available() for that session,
// so the caller may safely destroy the MediaSession.
class AudioWorkerPool : private NonCopyable, private NonMovable {
public:
    // frame_ms: audio frame duration from MediaConfig; the worker idle sleep
    // is derived from it (frame_ms / 40) so latency scales with frame size.
    AudioWorkerPool(size_t worker_count, uint32_t frame_ms, Logger& logger);
    ~AudioWorkerPool();

    bool start();
    void stop();

    // Assign a MediaSession to this pool.  Thread-safe; called at session creation.
    // Returns the worker index the session was assigned to.
    size_t assign(MediaSession* session);

    // Remove a session and WAIT until no worker is inside drain_available() for it.
    // Thread-safe.  Must be called before the MediaSession is destroyed.
    void unassign(MediaSession* session);

    [[nodiscard]] size_t worker_count()  const noexcept { return workers_.size(); }
    // O(1) atomic read — maintained by assign()/unassign().
    [[nodiscard]] size_t session_count() const noexcept {
        return session_count_.load(std::memory_order_relaxed);
    }

private:
    // Per-session bookkeeping kept alive by shared_ptr so worker snapshots can
    // safely hold a copy while unassign() erases from the vector.
    struct SessionEntry {
        MediaSession*       session;
        // Set to true by unassign() under sessions_mutex, checked by worker
        // under drain_mutex to guarantee the double-check-locking is sound.
        std::atomic<bool>   removed{false};
        // Held by the worker while drain_available() executes.
        // unassign() acquires this after marking removed=true to wait for
        // any in-progress drain to complete before returning to the caller.
        std::mutex          drain_mutex;
    };

    struct Worker {
        std::thread                                 thread;
        std::vector<std::shared_ptr<SessionEntry>>  sessions;  // guarded by sessions_mutex
        std::mutex                                  sessions_mutex;
    };

    void worker_loop(size_t idx);

    size_t              worker_count_;
    uint32_t            frame_ms_;
    Logger&             logger_;
    std::atomic<size_t> session_count_{0};
    std::vector<std::unique_ptr<Worker>> workers_;
    std::atomic<bool>   running_{false};
    std::atomic<size_t> round_robin_{0};
};

} // namespace voiceai
