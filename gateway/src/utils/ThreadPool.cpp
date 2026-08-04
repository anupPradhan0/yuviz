#include "utils/ThreadPool.h"

namespace voiceai {

ThreadPool::ThreadPool(size_t num_threads, std::string name)
    : name_(std::move(name))
{
    workers_.reserve(num_threads);
    for (size_t i = 0; i < num_threads; ++i) {
        workers_.emplace_back([this, i] { worker_loop(i); });
    }
}

ThreadPool::~ThreadPool() {
    shutdown();
}

void ThreadPool::shutdown() {
    if (stop_.exchange(true)) return;
    cv_.notify_all();
    for (auto& t : workers_) {
        if (t.joinable()) t.join();
    }
}

void ThreadPool::worker_loop(size_t /*id*/) {
    while (true) {
        std::function<void()> task;
        {
            std::unique_lock lock{mutex_};
            cv_.wait(lock, [this] {
                return stop_.load(std::memory_order_relaxed) || !tasks_.empty();
            });

            if (stop_.load(std::memory_order_relaxed) && tasks_.empty()) return;

            task = std::move(tasks_.front());
            tasks_.pop();
        }

        task();
        --pending_;
    }
}

} // namespace voiceai
