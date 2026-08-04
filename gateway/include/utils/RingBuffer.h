#pragma once

#include <atomic>
#include <cstring>
#include <memory>

namespace voiceai {

// Lock-free single-producer single-consumer ring buffer.
// Capacity is rounded up to the next power of two internally.
template<typename T>
class RingBuffer {
public:
    explicit RingBuffer(size_t capacity)
        : capacity_(next_power_of_two(capacity))
        , mask_(capacity_ - 1)
        , buffer_(std::make_unique<T[]>(capacity_))
        , write_pos_(0)
        , read_pos_(0)
    {}

    ~RingBuffer() = default;

    RingBuffer(const RingBuffer&)            = delete;
    RingBuffer& operator=(const RingBuffer&) = delete;

    // Producer side — call only from a single producer thread.
    bool push(const T* data, size_t count) {
        const size_t w     = write_pos_.load(std::memory_order_relaxed);
        const size_t r     = read_pos_.load(std::memory_order_acquire);
        const size_t avail = capacity_ - (w - r);
        if (avail < count) return false;

        const size_t w_idx = w & mask_;
        const size_t tail  = capacity_ - w_idx;
        if (count <= tail) {
            std::memcpy(buffer_.get() + w_idx, data, count * sizeof(T));
        } else {
            std::memcpy(buffer_.get() + w_idx, data, tail * sizeof(T));
            std::memcpy(buffer_.get(), data + tail, (count - tail) * sizeof(T));
        }
        write_pos_.store(w + count, std::memory_order_release);
        return true;
    }

    // Consumer side — call only from a single consumer thread.
    size_t pop(T* data, size_t max_count) {
        const size_t r     = read_pos_.load(std::memory_order_relaxed);
        const size_t w     = write_pos_.load(std::memory_order_acquire);
        const size_t avail = w - r;
        if (avail == 0) return 0;

        const size_t count = std::min(avail, max_count);
        const size_t r_idx = r & mask_;
        const size_t tail  = capacity_ - r_idx;
        if (count <= tail) {
            std::memcpy(data, buffer_.get() + r_idx, count * sizeof(T));
        } else {
            std::memcpy(data, buffer_.get() + r_idx, tail * sizeof(T));
            std::memcpy(data + tail, buffer_.get(), (count - tail) * sizeof(T));
        }
        read_pos_.store(r + count, std::memory_order_release);
        return count;
    }

    [[nodiscard]] size_t available() const noexcept {
        return write_pos_.load(std::memory_order_acquire)
             - read_pos_.load(std::memory_order_acquire);
    }

    [[nodiscard]] size_t free_space() const noexcept {
        return capacity_ - available();
    }

    [[nodiscard]] size_t capacity() const noexcept { return capacity_; }

    void clear() noexcept {
        read_pos_.store(write_pos_.load(std::memory_order_acquire),
                        std::memory_order_release);
    }

private:
    static size_t next_power_of_two(size_t n) {
        if (n == 0) return 1;
        size_t p = 1;
        while (p < n) p <<= 1;
        return p;
    }

    const size_t         capacity_;
    const size_t         mask_;
    std::unique_ptr<T[]> buffer_;
    alignas(64) std::atomic<size_t> write_pos_;   // T1 (producer) — own cache line
    alignas(64) std::atomic<size_t> read_pos_;    // T2 (consumer) — own cache line
};

} // namespace voiceai
