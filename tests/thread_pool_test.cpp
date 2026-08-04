#include <gtest/gtest.h>
#include "utils/ThreadPool.h"

#include <atomic>
#include <chrono>
#include <thread>

using namespace voiceai;
using namespace std::chrono_literals;

TEST(ThreadPoolTest, SubmitReturnsResultViaFuture) {
    ThreadPool pool{2, "test"};
    auto future = pool.submit([] { return 42; });
    EXPECT_EQ(future.get(), 42);
}

TEST(ThreadPoolTest, TasksRunConcurrentlyNotSerialized) {
    ThreadPool pool{2, "test"};
    const auto start = std::chrono::steady_clock::now();

    auto f1 = pool.submit([] { std::this_thread::sleep_for(100ms); return 1; });
    auto f2 = pool.submit([] { std::this_thread::sleep_for(100ms); return 2; });
    f1.get();
    f2.get();

    const auto elapsed = std::chrono::steady_clock::now() - start;
    // If serialized, this would take >=200ms; concurrent execution on 2
    // worker threads should finish in ~100ms. 150ms leaves headroom for
    // scheduling jitter without being loose enough to hide a regression.
    EXPECT_LT(elapsed, 150ms);
}

TEST(ThreadPoolTest, ShutdownDrainsQueuedTasksBeforeReturning) {
    ThreadPool pool{1, "test"};
    std::atomic<int> completed{0};

    // Single worker, several tasks queued back-to-back — shutdown() must not
    // return (and callers must not proceed to destroy what these tasks
    // reference) until every one of them has actually run.
    for (int i = 0; i < 5; ++i) {
        pool.submit([&completed] {
            std::this_thread::sleep_for(10ms);
            ++completed;
        });
    }
    pool.shutdown();

    EXPECT_EQ(completed.load(), 5);
}

TEST(ThreadPoolTest, SubmitAfterShutdownThrows) {
    ThreadPool pool{1, "test"};
    pool.shutdown();
    EXPECT_THROW(pool.submit([] {}), std::runtime_error);
}

TEST(ThreadPoolTest, ExceptionInTaskIsCapturedByFutureNotLost) {
    ThreadPool pool{1, "test"};
    auto future = pool.submit([]() -> int { throw std::runtime_error{"boom"}; });
    EXPECT_THROW(future.get(), std::runtime_error);
}
