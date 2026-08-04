#pragma once

#include <atomic>
#include <condition_variable>
#include <functional>
#include <future>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <vector>

namespace voiceai {

class ThreadPool {
public:
    explicit ThreadPool(size_t num_threads, std::string name = "pool");
    ~ThreadPool();

    ThreadPool(const ThreadPool&)            = delete;
    ThreadPool& operator=(const ThreadPool&) = delete;

    template<typename F, typename... Args>
    auto submit(F&& f, Args&&... args) -> std::future<std::invoke_result_t<F, Args...>> {
        using ReturnType = std::invoke_result_t<F, Args...>;

        auto task = std::make_shared<std::packaged_task<ReturnType()>>(
            [f = std::forward<F>(f), ...args = std::forward<Args>(args)]() mutable {
                return std::invoke(std::forward<F>(f), std::forward<Args>(args)...);
            });

        std::future<ReturnType> result = task->get_future();
        {
            std::unique_lock lock{mutex_};
            if (stop_.load(std::memory_order_relaxed))
                throw std::runtime_error("ThreadPool is stopped");
            tasks_.emplace([task]() { (*task)(); });
            ++pending_;
        }
        cv_.notify_one();
        return result;
    }

    void shutdown();

    [[nodiscard]] size_t size()          const noexcept { return workers_.size(); }
    [[nodiscard]] size_t pending_tasks() const noexcept { return pending_.load(); }

private:
    void worker_loop(size_t id);

    std::string                         name_;
    std::vector<std::thread>            workers_;
    std::queue<std::function<void()>>   tasks_;
    std::mutex                          mutex_;
    std::condition_variable             cv_;
    std::atomic<bool>                   stop_{false};
    std::atomic<size_t>                 pending_{0};
};

} // namespace voiceai
