#include <gtest/gtest.h>
#include "media/PlaybackQueue.h"
#include "logging/Logger.h"

#include <atomic>
#include <chrono>
#include <thread>

namespace {

using namespace voiceai;

// ── Stub metrics ──────────────────────────────────────────────────────────────

class StubMetrics final : public IMetrics {
public:
    bool initialize() override { return true; }
    bool start()      override { return true; }
    void stop()       override {}
    void shutdown()   override {}
    void increment(const std::string&, double = 1.0) override { ++increments; }
    void gauge(const std::string&, double)           override {}
    void observe(const std::string&, double)         override {}
    int increments{0};
};

// ── Fixture ───────────────────────────────────────────────────────────────────

class PlaybackQueueTest : public ::testing::Test {
protected:
    StubMetrics metrics_;
    Logger      logger_ = Logger::make_null();

    AudioFrame make_frame(const std::string& sid = "s1", size_t bytes = 640) {
        AudioFrame f;
        f.set_session_id(sid);
        f.direction  = AudioDirection::Outbound;
        f.payload.assign(bytes, 0);
        return f;
    }

    PlaybackQueue make_queue(size_t max = 10) {
        return PlaybackQueue{"s1", max, metrics_, logger_};
    }
};

// ── Tests ─────────────────────────────────────────────────────────────────────

TEST_F(PlaybackQueueTest, EmptyOnCreate) {
    auto q = make_queue();
    EXPECT_TRUE(q.is_empty());
    EXPECT_EQ(q.size(), 0u);
}

TEST_F(PlaybackQueueTest, PushAndPop) {
    auto q = make_queue();
    q.push(make_frame());
    EXPECT_EQ(q.size(), 1u);

    auto f = q.pop(std::chrono::milliseconds{10});
    ASSERT_TRUE(f.has_value());
    EXPECT_EQ(f->get_session_id(), "s1");
    EXPECT_TRUE(q.is_empty());
}

TEST_F(PlaybackQueueTest, FifoOrder) {
    auto q = make_queue(5);
    for (size_t i = 0; i < 3; ++i) {
        AudioFrame f;
        f.payload.push_back(static_cast<uint8_t>(i));
        q.push(std::move(f));
    }
    for (uint8_t expected = 0; expected < 3; ++expected) {
        auto f = q.pop(std::chrono::milliseconds{10});
        ASSERT_TRUE(f.has_value());
        EXPECT_EQ(f->payload[0], expected);
    }
}

TEST_F(PlaybackQueueTest, RejectNewestOnOverflow) {
    auto q = make_queue(2);
    AudioFrame a; a.payload = {1};
    AudioFrame b; b.payload = {2};
    AudioFrame c; c.payload = {3};

    q.push(std::move(a));
    q.push(std::move(b));
    q.push(std::move(c));  // 'c' rejected — queued speech must not be corrupted

    EXPECT_EQ(q.size(), 2u);
    EXPECT_GT(metrics_.increments, 0);  // playback_queue.dropped was incremented

    auto first = q.pop(std::chrono::milliseconds{10});
    ASSERT_TRUE(first.has_value());
    EXPECT_EQ(first->payload[0], 1);  // oldest frame survives intact

    // Space freed — pushes accepted again.
    AudioFrame d; d.payload = {4};
    q.push(std::move(d));
    EXPECT_EQ(q.size(), 2u);
}

TEST_F(PlaybackQueueTest, ClearDiscardsAll) {
    auto q = make_queue();
    q.push(make_frame());
    q.push(make_frame());
    q.clear();
    EXPECT_TRUE(q.is_empty());
}

TEST_F(PlaybackQueueTest, PopTimesOutWhenEmpty) {
    auto q = make_queue();
    const auto start = std::chrono::steady_clock::now();
    auto result = q.pop(std::chrono::milliseconds{20});
    const auto elapsed = std::chrono::steady_clock::now() - start;
    EXPECT_FALSE(result.has_value());
    EXPECT_GE(std::chrono::duration_cast<std::chrono::milliseconds>(elapsed).count(), 15);
}

TEST_F(PlaybackQueueTest, StopUnblocksConsumer) {
    auto q = make_queue();
    std::atomic<bool> done{false};

    std::thread consumer([&] {
        q.pop(std::chrono::milliseconds{5000});  // would block for 5 s
        done.store(true);
    });

    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    q.stop();
    consumer.join();
    EXPECT_TRUE(done.load());
}

TEST_F(PlaybackQueueTest, DrainCallbackFiredWhenQueueEmpties) {
    auto q = make_queue();
    std::atomic<bool> drained{false};
    q.set_on_drained([&] { drained.store(true); });

    q.push(make_frame());
    q.pop(std::chrono::milliseconds{10});  // empties the queue → callback fires
    EXPECT_TRUE(drained.load());
}

TEST_F(PlaybackQueueTest, FillPctReported) {
    auto q = make_queue(4);
    EXPECT_EQ(q.fill_pct(), 0.0f);
    q.push(make_frame());
    q.push(make_frame());
    EXPECT_FLOAT_EQ(q.fill_pct(), 50.0f);
}

} // namespace
