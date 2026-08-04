#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <optional>

namespace voiceai {

// Lock-free single-producer single-consumer bounded queue.
//
// Invariant: push() is called by exactly one thread (the producer) and
// pop() is called by exactly one different thread (the consumer).  Violating
// this invariant is undefined behaviour.
//
// N must be a power of two.  When the queue is full, push() drops the item
// and returns false.  Cache-line padding separates head_ and tail_ to
// avoid false sharing.
template<typename T, size_t N>
class SPSCQueue {
    static_assert(N >= 2 && (N & (N - 1)) == 0, "N must be a power of two >= 2");

    std::array<T, N>                buf_{};
    alignas(64) std::atomic<size_t> head_{0};  // owned by consumer
    alignas(64) std::atomic<size_t> tail_{0};  // owned by producer

public:
    // Called by the producer thread.
    // Returns false if the queue is full (item is discarded).
    bool push(T item) noexcept {
        const size_t t = tail_.load(std::memory_order_relaxed);
        if (t - head_.load(std::memory_order_acquire) >= N)
            return false;
        buf_[t & (N - 1)] = std::move(item);
        tail_.store(t + 1, std::memory_order_release);
        return true;
    }

    // Called by the consumer thread.
    // Returns nullopt when empty.
    std::optional<T> pop() noexcept {
        const size_t h = head_.load(std::memory_order_relaxed);
        if (tail_.load(std::memory_order_acquire) == h)
            return std::nullopt;
        T item = std::move(buf_[h & (N - 1)]);
        head_.store(h + 1, std::memory_order_release);
        return item;
    }
};

} // namespace voiceai
