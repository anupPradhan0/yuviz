#include <gtest/gtest.h>
#include "utils/RingBuffer.h"
#include <numeric>
#include <thread>
#include <vector>

namespace {

TEST(RingBufferTest, PushAndPop) {
    voiceai::RingBuffer<int16_t> rb{16};
    int16_t in[]   = {1, 2, 3, 4};
    int16_t out[4] = {};

    EXPECT_TRUE(rb.push(in, 4));
    EXPECT_EQ(rb.available(), 4u);
    EXPECT_EQ(rb.pop(out, 4), 4u);
    EXPECT_EQ(out[0], 1);
    EXPECT_EQ(out[3], 4);
    EXPECT_EQ(rb.available(), 0u);
}

TEST(RingBufferTest, WrapAround) {
    voiceai::RingBuffer<int16_t> rb{8};
    int16_t data[6] = {10, 20, 30, 40, 50, 60};
    int16_t out[6]  = {};

    // Push 6, pop 4 → {50,60} remain at tail of ring, write_pos=6, read_pos=4
    EXPECT_TRUE(rb.push(data, 6));
    EXPECT_EQ(rb.pop(out, 4), 4u);

    // Push 6 more — wraps physically around the ring, write_pos=12
    EXPECT_TRUE(rb.push(data, 6));

    // Pop 6: first two are the leftover {50,60}, then the wrapped {10,20,30,40}
    EXPECT_EQ(rb.pop(out, 6), 6u);
    EXPECT_EQ(out[0], 50);
    EXPECT_EQ(out[1], 60);
    EXPECT_EQ(out[2], 10);
    EXPECT_EQ(out[5], 40);
}

TEST(RingBufferTest, ReturnsFalseWhenFull) {
    voiceai::RingBuffer<int16_t> rb{4};
    int16_t data[5] = {};
    EXPECT_FALSE(rb.push(data, 5));
}

TEST(RingBufferTest, ClearResetsBuffer) {
    voiceai::RingBuffer<int16_t> rb{8};
    int16_t data[4] = {1, 2, 3, 4};
    rb.push(data, 4);
    rb.clear();
    EXPECT_EQ(rb.available(), 0u);
}

TEST(RingBufferTest, SPSCConcurrency) {
    constexpr size_t N = 10000;
    voiceai::RingBuffer<int32_t> rb{N * 2};

    std::vector<int32_t> produced(N);
    std::iota(produced.begin(), produced.end(), 0);

    std::vector<int32_t> consumed;
    consumed.reserve(N);

    std::thread producer([&] {
        size_t sent = 0;
        while (sent < N) {
            if (rb.push(&produced[sent], 1)) ++sent;
        }
    });

    std::thread consumer([&] {
        int32_t val{};
        while (consumed.size() < N) {
            if (rb.pop(&val, 1) == 1) consumed.push_back(val);
        }
    });

    producer.join();
    consumer.join();

    EXPECT_EQ(consumed, produced);
}

} // namespace
