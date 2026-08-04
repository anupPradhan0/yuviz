#include <gtest/gtest.h>
#include "config/Config.h"
#include "config/RedisClient.h"
#include "logging/Logger.h"
#include <fstream>
#include <filesystem>

namespace {

class ConfigTest : public ::testing::Test {
protected:
    std::filesystem::path tmp_yaml_;

    void SetUp() override {
        tmp_yaml_ = std::filesystem::temp_directory_path() / "test_gateway.yaml";
    }

    void TearDown() override {
        std::filesystem::remove(tmp_yaml_);
    }

    void write_yaml(const std::string& content) {
        std::ofstream f{tmp_yaml_};
        f << content;
    }
};

TEST_F(ConfigTest, DefaultsAreAppliedWhenKeysMissing) {
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    EXPECT_EQ(cfg.websocket().port, 8080);
    EXPECT_EQ(cfg.websocket().host, "0.0.0.0");
    EXPECT_EQ(cfg.media().sample_rate, 16000u);
    EXPECT_EQ(cfg.media().channels, 1u);
}

TEST_F(ConfigTest, ValuesOverrideDefaults) {
    write_yaml(R"(
gateway:
  websocket:
    port: 9090
    host: "127.0.0.1"
    max_connections: 500
  media:
    sample_rate: 16000
    channels: 1
    frame_ms: 40
)");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    EXPECT_EQ(cfg.websocket().port, 9090);
    EXPECT_EQ(cfg.websocket().host, "127.0.0.1");
    EXPECT_EQ(cfg.websocket().max_connections, 500u);
    EXPECT_EQ(cfg.media().sample_rate, 16000u);
    EXPECT_EQ(cfg.media().frame_ms, 40u);
}

TEST_F(ConfigTest, ThrowsOnMissingFile) {
    voiceai::Config cfg;
    EXPECT_THROW(cfg.load("/nonexistent/path/config.yaml"), std::runtime_error);
}

// ── TenantConfig::from_default() ────────────────────────────────────────────

TEST_F(ConfigTest, FromDefaultCopiesAudioParams) {
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    const auto tc = voiceai::TenantConfig::from_default(cfg.gateway());

    EXPECT_EQ(tc.sample_rate, cfg.media().sample_rate);
    EXPECT_EQ(tc.channels,    cfg.media().channels);
    EXPECT_EQ(tc.frame_ms,    cfg.media().frame_ms);
}

TEST_F(ConfigTest, FromDefaultDerivesInboundFrameCapFromRingBufferMs) {
    // Default: ring_buffer_ms=500, frame_ms=20 → 500/20 = 25 frames
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    const auto tc = voiceai::TenantConfig::from_default(cfg.gateway());

    const size_t expected = cfg.media().ring_buffer_ms / cfg.media().frame_ms;
    EXPECT_EQ(tc.backpressure.max_inbound_queue_frames, expected);
}

TEST_F(ConfigTest, FromDefaultInboundFrameCapScalesWithFrameMs) {
    // frame_ms=40 → ring_buffer_ms(500)/40 = 12 frames
    write_yaml(R"(
gateway:
  media:
    frame_ms: 40
    ring_buffer_ms: 500
)");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    const auto tc = voiceai::TenantConfig::from_default(cfg.gateway());
    EXPECT_EQ(tc.backpressure.max_inbound_queue_frames, 500u / 40u);
}

TEST_F(ConfigTest, FromDefaultOutboundFrameCapCopiedFromPlaybackMaxFrames) {
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    const auto tc = voiceai::TenantConfig::from_default(cfg.gateway());
    EXPECT_EQ(tc.backpressure.max_outbound_queue_frames,
              cfg.media().playback_max_frames);
}

TEST_F(ConfigTest, ConversationSectionParsedFromYaml) {
    write_yaml(R"(
gateway:
  conversation:
    type: "grpc"
    endpoint: "10.0.0.1:50051"
    connect_timeout_ms: 3000
)");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    EXPECT_EQ(cfg.conversation().type,               "grpc");
    EXPECT_EQ(cfg.conversation().endpoint,           "10.0.0.1:50051");
    EXPECT_EQ(cfg.conversation().connect_timeout_ms, 3000u);
}

// ── Redis section (Phase 5) ──────────────────────────────────────────────────

TEST_F(ConfigTest, RedisSectionDefaultsToDisabled) {
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    EXPECT_FALSE(cfg.gateway().redis.enabled);
    EXPECT_EQ(cfg.gateway().redis.port, 6379);
}

TEST_F(ConfigTest, RedisSectionParsedFromYaml) {
    write_yaml(R"(
gateway:
  redis:
    enabled: true
    host: "10.0.0.5"
    port: 6380
    connect_timeout_ms: 300
    command_timeout_ms: 150
)");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    EXPECT_TRUE(cfg.gateway().redis.enabled);
    EXPECT_EQ(cfg.gateway().redis.host, "10.0.0.5");
    EXPECT_EQ(cfg.gateway().redis.port, 6380);
    EXPECT_EQ(cfg.gateway().redis.connect_timeout_ms, 300u);
    EXPECT_EQ(cfg.gateway().redis.command_timeout_ms, 150u);
}

// ── TenantConfig::from_redis() ───────────────────────────────────────────────
// Only the disabled-Redis path is exercised here — this test suite is
// hermetic by convention (no other test connects to a live external service;
// EslClient has no dedicated test file for the same reason). The live
// Redis roundtrip is verified manually, not via ctest.

TEST_F(ConfigTest, FromRedisFallsBackToDefaultsWhenRedisDisabled) {
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    voiceai::Logger logger{"test"};
    voiceai::RedisClient redis{cfg.gateway().redis, logger};  // enabled=false by default

    const auto expected = voiceai::TenantConfig::from_default(cfg.gateway());
    const auto actual   = voiceai::TenantConfig::from_redis(redis, "default", cfg.gateway());

    EXPECT_EQ(actual.sample_rate,  expected.sample_rate);
    EXPECT_EQ(actual.vad_engine,   expected.vad_engine);
    EXPECT_EQ(actual.silero.hold_ms, expected.silero.hold_ms);
    EXPECT_EQ(actual.tenant_id, "default");
}

// ── PhoneRoute::from_redis() ─────────────────────────────────────────────────
// Same hermetic convention as FromRedisFallsBackToDefaultsWhenRedisDisabled
// above — only the disabled/missing-key fallback path is exercised here; the
// live Redis roundtrip is verified manually, not via ctest.

TEST_F(ConfigTest, PhoneRouteFallsBackToDefaultsWhenRedisDisabled) {
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    voiceai::Logger logger{"test"};
    voiceai::RedisClient redis{cfg.gateway().redis, logger};  // enabled=false by default

    const auto route = voiceai::PhoneRoute::from_redis(redis, "5000");

    EXPECT_EQ(route.tenant_slug, "default");
    EXPECT_EQ(route.agent_slug, "default");
    EXPECT_EQ(route.version, 0u);
}

TEST_F(ConfigTest, PhoneRouteFallsBackToDefaultsOnEmptyDid) {
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    voiceai::Logger logger{"test"};
    voiceai::RedisClient redis{cfg.gateway().redis, logger};

    const auto route = voiceai::PhoneRoute::from_redis(redis, "");

    EXPECT_EQ(route.tenant_slug, "default");
    EXPECT_EQ(route.agent_slug, "default");
    EXPECT_EQ(route.version, 0u);
}

// ── CallMetadata::parse() ────────────────────────────────────────────────────
// Pure function, no I/O — hermetic by construction, not just by convention.

TEST_F(ConfigTest, CallMetadataParsesAllFields) {
    const auto md = voiceai::CallMetadata::parse(
        std::string(R"({"type":"start","call_id":"c1","did":"5000","ani":"5551234567","direction":"inbound"})"));

    EXPECT_EQ(md.did, "5000");
    EXPECT_EQ(md.ani, "5551234567");
    EXPECT_EQ(md.direction, "inbound");
}

TEST_F(ConfigTest, CallMetadataFallsBackOnMissingFrame) {
    const auto md = voiceai::CallMetadata::parse(std::nullopt);

    EXPECT_EQ(md.did, "");
    EXPECT_EQ(md.ani, "");
    EXPECT_EQ(md.direction, "inbound");
}

TEST_F(ConfigTest, CallMetadataFallsBackOnMalformedJson) {
    const auto md = voiceai::CallMetadata::parse(std::string("not json at all"));

    EXPECT_EQ(md.did, "");
    EXPECT_EQ(md.ani, "");
    EXPECT_EQ(md.direction, "inbound");
}

TEST_F(ConfigTest, CallMetadataDefaultsMissingFieldsIndividually) {
    const auto md = voiceai::CallMetadata::parse(std::string(R"({"did":"5001"})"));

    EXPECT_EQ(md.did, "5001");
    EXPECT_EQ(md.ani, "");           // not present — stays default
    EXPECT_EQ(md.direction, "inbound"); // not present — stays default
}

TEST_F(ConfigTest, CallMetadataIgnoresWrongTypedFields) {
    const auto md = voiceai::CallMetadata::parse(std::string(R"({"did":12345,"ani":"5551234567"})"));

    EXPECT_EQ(md.did, "");           // wrong type (number, not string) — stays default
    EXPECT_EQ(md.ani, "5551234567"); // correctly typed field still parses
}

} // namespace

// ── CallFsmTimerConfig transfer timeout (Phase 5F) ───────────────────────────
// The Redis overlay's bounds logic itself needs a live/fake Redis (see the
// hermetic-suite convention above) — verified live instead. What IS asserted
// hermetically: the compiled default and the bound ordering the overlay
// clamps against.

TEST_F(ConfigTest, TransferTimeoutDefaultIs45sWithSaneBounds) {
    const voiceai::CallFsmTimerConfig t{};
    EXPECT_EQ(t.transfer_timeout, std::chrono::milliseconds{45'000});
    EXPECT_EQ(t.transfer_timeout, voiceai::CallFsmTimerConfig::transfer_timeout_default);
    EXPECT_LT(voiceai::CallFsmTimerConfig::transfer_timeout_min,
              voiceai::CallFsmTimerConfig::transfer_timeout_default);
    EXPECT_LT(voiceai::CallFsmTimerConfig::transfer_timeout_default,
              voiceai::CallFsmTimerConfig::transfer_timeout_max);
}

TEST_F(ConfigTest, NoSpeechTimeoutDefaultIs30sWithSaneBounds) {
    const voiceai::CallFsmTimerConfig t{};
    EXPECT_EQ(t.no_speech_timeout, std::chrono::milliseconds{30'000});
    EXPECT_EQ(t.no_speech_timeout, voiceai::CallFsmTimerConfig::no_speech_timeout_default);
    EXPECT_LT(voiceai::CallFsmTimerConfig::no_speech_timeout_min,
              voiceai::CallFsmTimerConfig::no_speech_timeout_default);
    EXPECT_LT(voiceai::CallFsmTimerConfig::no_speech_timeout_default,
              voiceai::CallFsmTimerConfig::no_speech_timeout_max);
}
