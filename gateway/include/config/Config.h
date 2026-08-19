#pragma once

#include "media/EnergyVADConfig.h"
#include "media/SileroVADConfig.h"
#include "session/CallFsmTimerConfig.h"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>

namespace voiceai {

struct WebSocketConfig {
    std::string host{"0.0.0.0"};
    uint16_t    port{8080};
    uint32_t    max_connections{1000};
    uint32_t    timeout_ms{30000};

    // Bounded wait for mod_audio_fork's metadata text frame (DID/ANI/
    // direction) before falling back to PhoneRoute's {"default","default"}.
    // Real calls resolve near-instantly — mod_audio_fork sends metadata as
    // the guaranteed first write, before any audio — so this is headroom
    // for a connection that never sends one (misconfigured caller, a raw
    // test client), not an expected steady-state delay.
    uint32_t    metadata_wait_ms{300};
};

struct LoggingConfig {
    std::string level{"info"};
    std::string file{};
    bool        console{true};
};

struct MediaConfig {
    uint32_t sample_rate{16'000};  // 16 kHz L16 from mod_audio_fork
    uint8_t  channels{1};
    uint32_t frame_ms{20};
    uint32_t ring_buffer_ms{500};
    uint32_t playback_max_frames{3000}; // 60s at 20ms/frame — matches playback_timeout
    std::string vad_engine{"silero"};   // "silero" | "energy"
    std::string vad_model_path{"models/silero_vad.onnx"};
};

struct WorkerConfig {
    uint32_t audio_worker_threads{4};  // AudioWorkerPool thread count
};

struct MetricsConfig {
    bool        enabled{false};
    uint16_t    port{9090};
    std::string path{"/metrics"};
};

struct ConversationTransportConfig {
    std::string type{"null"};      // "null" | "grpc"
    std::string endpoint{"localhost:50051"};
    uint32_t    connect_timeout_ms{5000};
};

// FreeSWITCH Event Socket Library — used to issue call-control commands
// (currently just uuid_kill for agent-initiated hangup) that the WebSocket/
// gRPC path has no way to express.  Disabled by default: an agent deciding
// to end the call still closes the WebSocket/RTP audio path either way, so
// leaving this off only means the SIP leg itself lingers until the caller
// hangs up — a safe degradation, not a broken state.
struct EslConfig {
    bool        enabled{false};
    std::string host{"127.0.0.1"};
    uint16_t    port{8021};
    std::string password{"ClueCon"};   // FreeSWITCH default; override in production
    uint32_t    connect_timeout_ms{2000};

    // Warm transfer's plain-extension destinations (e.g. "1001") are dialed
    // as sofia/external/sip:<dest>@<sip_proxy_host>:<sip_proxy_port> — see
    // EslClient::originate_async(). This is deliberately NOT FreeSWITCH's
    // own "user/<id>" channel type: in any deployment fronted by a SIP
    // proxy/registrar (Kamailio, here — see docs/warm_transfer_architecture.md
    // §6), real phones register with the proxy, not with FreeSWITCH, so
    // FreeSWITCH's own directory has no knowledge of them and "user/<id>"
    // fails with USER_NOT_REGISTERED even when the destination is genuinely
    // online. Defaults are non-functional placeholders (matching esl.enabled's
    // own "safe until configured" pattern) — set explicitly per deployment.
    std::string sip_proxy_host{"127.0.0.1"};
    uint16_t    sip_proxy_port{5060};
};

// Redis — the Phase 5 config-plane cache (see project_phase5_schema_design.md).
// Disabled by default: TenantConfig::from_redis() degrades to from_default()
// on any connect/lookup/parse failure, so a missing or unreachable Redis
// never rejects a call — same "safe degradation" discipline as EslConfig.
struct RedisConfig {
    bool        enabled{false};
    std::string host{"127.0.0.1"};
    uint16_t    port{6379};
    uint32_t    connect_timeout_ms{200};
    uint32_t    command_timeout_ms{100};
    uint32_t    pool_size{4};
};

struct GatewayConfig {
    WebSocketConfig             websocket;
    LoggingConfig               logging;
    ConversationTransportConfig conversation;  // AI service transport
    MediaConfig                 media;
    WorkerConfig                workers;
    MetricsConfig               metrics;
    EslConfig                   esl;
    RedisConfig                 redis;
};

// Limits on inbound and outbound queue depth.
struct BackpressureConfig {
    size_t max_inbound_queue_frames{25};    // ring buffer capacity (25 * 20ms = 500ms default)
    size_t max_outbound_queue_frames{3000}; // PlaybackQueue capacity (60s at 20ms/frame)
};

// Per-session configuration snapshot — Principle 6.
//
// Bound once at call creation; never mutated during a call.
// Populated per-tenant via from_redis() (see below), falling back to
// from_default(GatewayConfig) process defaults on any cache/parse failure.
//
// All sub-configs mirror the types their consumers accept directly, so
// construction becomes a single assignment rather than field-by-field copy.
struct TenantConfig {
    std::string tenant_id;

    uint32_t sample_rate{16'000};
    uint8_t  channels{1};
    uint32_t frame_ms{20};

    std::string                 vad_engine{"silero"};  // "silero" | "energy"
    EnergyVADConfig             vad;      // fallback engine
    SileroVADConfig             silero;
    CallFsmTimerConfig          timers;
    ConversationTransportConfig transport;
    BackpressureConfig          backpressure;

    // Build a TenantConfig from GatewayConfig process-level defaults.
    [[nodiscard]] static TenantConfig from_default(const GatewayConfig& cfg) noexcept;

    // Phase 5: look up `tenant:{tenant_id}` in Redis (the same key + JSON
    // shape services/config/tenants.py writes) and overlay any present
    // fields onto from_default()'s baseline. On a cache miss, a Redis
    // connection/command failure, or a JSON parse error, silently falls back
    // to from_default(cfg) in full — a config-plane outage must never reject
    // a call (see project_phase5_schema_design.md's Redis cache strategy).
    // logger (optional): used only to surface config-validation warnings
    // (e.g. an out-of-bounds transfer_timeout_ms falling back to default);
    // resolution behavior is identical with or without it.
    [[nodiscard]] static TenantConfig from_redis(
        class RedisClient&  redis,
        const std::string&  tenant_id,
        const GatewayConfig& cfg,
        class Logger*        logger = nullptr) noexcept;
};

// DID → tenant/agent routing result. Resolved once per new WebSocket
// connection (see Application.cpp's connect handler), before TenantConfig
// itself is resolved — the tenant_slug this produces is what gets passed to
// TenantConfig::from_redis().
//
// Defaults to {"default", "default"} — the same safe-degrade contract as
// TenantConfig::from_redis(): an unknown DID, a Redis miss, a connection
// failure, or a malformed cache entry must never reject a call, they just
// mean the call resolves to the same 'default' tenant/agent every unrouted
// call used before this feature existed.
struct PhoneRoute {
    std::string tenant_slug{"default"};
    std::string agent_slug{"default"};
    // Resolved agent's config_version (see agents.config_version, bumped by
    // a trigger on every UPDATE) — 0 when absent (agent_slug fell through to
    // the literal 'default' with no real agent row, or the field was
    // missing/null). Observability only today (logged, not yet acted on) —
    // lets a future consumer detect "this route was cached against an
    // older agent config" without a second lookup.
    uint32_t    version{0};

    // Looks up `did:{did}` in Redis (the same key + JSON shape
    // services/config/phone_numbers.py's get_by_did() writes:
    // {"tenant_slug": ..., "agent_slug": ..., "version": ...}, already
    // denormalized so this hot path never needs a second Postgres join).
    [[nodiscard]] static PhoneRoute from_redis(
        class RedisClient& redis, const std::string& did) noexcept;
};

// Parsed from the WS text frame mod_audio_fork sends (via its `metadata`
// uuid_audio_fork argument) before any audio, carrying the DID/ANI/direction
// the Lua dialplan script collected from FreeSWITCH channel variables.
//
// Never throws; a missing frame (no metadata sent, or the connection closed
// before it arrived) or malformed/partial JSON both degrade to the same
// {"", "", "inbound"} baseline — PhoneRoute::from_redis("") behaves exactly
// like an unknown DID, falling back to {"default","default"} — never a
// rejected call.
struct CallMetadata {
    std::string did;              // called number — feeds PhoneRoute::from_redis()
    std::string ani;              // calling number — feeds SessionContext::caller_did
    std::string direction{"inbound"};

    // raw is nullopt when no text frame arrived in time (or the connection
    // closed first) — treated identically to a present-but-malformed frame:
    // full default CallMetadata{}.
    [[nodiscard]] static CallMetadata parse(
        const std::optional<std::string>& raw) noexcept;
};

class Config {
public:
    Config() = default;
    ~Config() = default;

    Config(const Config&)            = delete;
    Config& operator=(const Config&) = delete;

    void load(const std::string& path);

    [[nodiscard]] const GatewayConfig&               gateway()      const noexcept { return config_; }
    [[nodiscard]] const WebSocketConfig&             websocket()    const noexcept { return config_.websocket; }
    [[nodiscard]] const LoggingConfig&               logging()      const noexcept { return config_.logging; }
    [[nodiscard]] const ConversationTransportConfig& conversation() const noexcept { return config_.conversation; }
    [[nodiscard]] const MediaConfig&                 media()        const noexcept { return config_.media; }
    [[nodiscard]] const MetricsConfig&               metrics()      const noexcept { return config_.metrics; }
    [[nodiscard]] const EslConfig&                   esl()          const noexcept { return config_.esl; }

private:
    GatewayConfig config_;
};

} // namespace voiceai
