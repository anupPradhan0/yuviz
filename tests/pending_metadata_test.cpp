#include <gtest/gtest.h>
#include "core/PendingMetadata.h"

#include <chrono>
#include <optional>
#include <thread>

using namespace voiceai;
using namespace std::chrono_literals;

TEST(PendingMetadataTest, TextArrivingBeforeWaitReturnsItImmediately) {
    PendingMetadata pending;
    pending.fulfill_with_text("hello");

    const auto result = pending.wait_for(200ms);
    ASSERT_TRUE(result.has_value());
    EXPECT_EQ(*result, "hello");
}

TEST(PendingMetadataTest, TextArrivingDuringWaitIsObserved) {
    PendingMetadata pending;

    std::thread producer([&pending] {
        std::this_thread::sleep_for(20ms);
        pending.fulfill_with_text("late arrival");
    });

    const auto result = pending.wait_for(200ms);
    producer.join();

    ASSERT_TRUE(result.has_value());
    EXPECT_EQ(*result, "late arrival");
}

TEST(PendingMetadataTest, CloseBeforeTextResolvesToNullopt) {
    PendingMetadata pending;
    pending.fulfill_with_close();

    const auto result = pending.wait_for(200ms);
    EXPECT_FALSE(result.has_value());
}

TEST(PendingMetadataTest, NeitherArrivesTimesOutToNullopt) {
    PendingMetadata pending;

    const auto start = std::chrono::steady_clock::now();
    const auto result = pending.wait_for(50ms);
    const auto elapsed = std::chrono::steady_clock::now() - start;

    EXPECT_FALSE(result.has_value());
    // Must actually wait out the timeout, not return instantly — proves the
    // bounded wait is real, not a no-op.
    EXPECT_GE(elapsed, 50ms);
    // But never hang past it either.
    EXPECT_LT(elapsed, 150ms);
}

TEST(PendingMetadataTest, FirstResolutionWinsTextThenClose) {
    PendingMetadata pending;
    pending.fulfill_with_text("first");
    pending.fulfill_with_close();   // must be a no-op — already resolved

    const auto result = pending.wait_for(200ms);
    ASSERT_TRUE(result.has_value());
    EXPECT_EQ(*result, "first");
}

TEST(PendingMetadataTest, FirstResolutionWinsCloseThenText) {
    PendingMetadata pending;
    pending.fulfill_with_close();
    pending.fulfill_with_text("too late");   // must be a no-op — already resolved

    const auto result = pending.wait_for(200ms);
    EXPECT_FALSE(result.has_value());
}
