#pragma once

#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "common/SystemClock.h"
#include "config/Config.h"
#include "config/RedisClient.h"
#include "logging/Logger.h"
#include "telephony/EslClient.h"
#include "telephony/EslEventListener.h"
#include "telephony/TransferCorrelator.h"
#include "transport/ConversationTransportFactory.h"
#include "utils/ThreadPool.h"

#include <atomic>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>

namespace voiceai {

// Forward declarations: avoid pulling heavy data-plane headers into every
// translation unit that includes Application.h.
class AudioWorkerPool;
class IComponent;
class IDispatcher;
class IMetrics;
class IWebSocketServer;
class SessionManager;
class TimerService;

// Single owner of all subsystems.
//
// Startup order:  Config → Logger → Metrics → Dispatcher
//                 → AudioWorkerPool → TimerService
//                 → ConversationTransportFactory (providers registered)
//                 → WebSocketServer
// Shutdown order: WebSocketServer → sessions cleared → TimerService
//                 → AudioWorkerPool → Dispatcher → Metrics
//
// Principle 3 (no Singleton): ConversationTransportFactory is constructed
// here and injected where needed — never accessed via a global instance().
//
// Principle 4 (no process globals): the signal handler reaches this instance
// via Application::s_active_, which is set in the constructor and cleared in
// the destructor.  POSIX guarantees at most one process-level signal handler
// per signal, so s_active_ is inherently single-valued.
//
// Principle 6 (TenantConfig): session creation snapshots a TenantConfig via
// the per-tenant Redis lookup (TenantConfig::from_redis), falling back to
// GatewayConfig defaults on any config-plane failure.
class Application : private NonCopyable, private NonMovable {
public:
    explicit Application(std::string config_path);
    ~Application();

    int  run();
    void shutdown();

    // Expose factory so callers (e.g. main.cpp) can register additional
    // transport providers (e.g. "grpc") without linking gRPC into gateway_lib.
    ConversationTransportFactory& transport_factory() noexcept {
        return transport_factory_;
    }

private:
    static void        signal_handler(int sig) noexcept;
    static Application* s_active_;   // for signal_handler; POSIX one-per-process

    void setup_signals();
    void initialize();
    void teardown();
    void wire_websocket_handlers();

    [[nodiscard]] size_t session_count() const;

    std::string config_path_;

    std::shared_ptr<const GatewayConfig> config_data_;
    std::unique_ptr<Logger>              logger_;
    SystemClock                          clock_;

    // ── Control-plane components ─────────────────────────────────────────────
    std::unique_ptr<IMetrics>         metrics_;
    std::unique_ptr<IDispatcher>      dispatcher_;
    std::unique_ptr<IWebSocketServer> ws_server_;

    // ── Data-plane components ────────────────────────────────────────────────
    std::unique_ptr<AudioWorkerPool>  audio_worker_pool_;
    std::unique_ptr<TimerService>     timer_service_;

    // ── Telephony control ────────────────────────────────────────────────────
    // One persistent ESL connection shared across sessions (see EslClient);
    // injected by reference into CallSessionFactory like the other services.
    std::unique_ptr<EslClient>        esl_client_;

    // uuid -> pending-transfer-resolution registry, shared by every
    // CallSession (via CallSessionFactory) and esl_event_listener_ below —
    // see TransferCorrelator's doc comment. Declared before
    // esl_event_listener_/session_manager_ so it outlives both (reverse
    // declaration-order destruction).
    //
    // Reused for two distinct uuid keyspaces (see
    // docs/warm_transfer_architecture.md §3's TransferContextRegistry —
    // deliberately implemented as two TransferCorrelator instances rather
    // than a new class: uuids are globally unique regardless of which
    // physical thing they name, so "watch this uuid" already works
    // identically for a caller's leg (cold), a warm transfer's agent leg
    // (CHANNEL_ANSWER/CHANNEL_HANGUP), or a bgapi Job-UUID (BACKGROUND_JOB)
    // — no need to invent a second registry class to get a second
    // keyspace, just a second instance):
    //   transfer_correlator_ — channel/leg uuids (cold's caller leg,
    //                          warm's agent leg)
    //   job_correlator_      — bgapi Job-UUIDs (warm's originate_async)
    TransferCorrelator                transfer_correlator_;
    TransferCorrelator                job_correlator_;

    // Separate ESL connection subscribed to CHANNEL_HANGUP/CHANNEL_BRIDGE
    // events, for real-time caller-hangup detection and transfer-outcome
    // correlation — see EslEventListener's doc comment for why this can't
    // share esl_client_'s connection.
    std::unique_ptr<EslEventListener> esl_event_listener_;

    // ── Config-plane cache ───────────────────────────────────────────────────
    // Phase 5: queried once per new WebSocket connection in
    // wire_websocket_handlers() to build each session's TenantConfig — not
    // injected into CallSessionFactory, since only session *creation* needs
    // it, not anything CallSession itself does afterward.
    std::unique_ptr<RedisClient>      redis_client_;

    // TenantConfig::from_redis() is a blocking call (hiredis is synchronous).
    // Running it directly on the libwebsockets service thread — the one
    // thread pumping I/O for every live call, not just new ones — would
    // stall every other concurrent call's audio for up to
    // connect_timeout_ms + command_timeout_ms on every single new call
    // setup. This small pool moves that blocking lookup (and the resulting
    // session_manager_->create() call) off the shared ws thread entirely.
    // Must be drained (shutdown()) in teardown() before redis_client_ and
    // session_manager_ are destroyed — see teardown()'s ordering comment.
    std::unique_ptr<ThreadPool>       config_resolver_pool_;

    // ~CallSession() joins its control/drain/gRPC threads and (see its own
    // destructor comment) may briefly block waiting for an in-flight
    // transfer coordinator to finish — neither of which the shared lws
    // thread (set_on_disconnect fires there) can afford to stall on
    // without freezing every other live call's I/O for the same duration.
    // A separate pool from config_resolver_pool_ above: that one also
    // backs new-session setup, and a slow teardown must never queue behind
    // (or make wait behind) an unrelated new call's own setup, or vice
    // versa. Drained in teardown() before session_manager_ is destroyed,
    // same ordering discipline as config_resolver_pool_.
    std::unique_ptr<ThreadPool>       session_cleanup_pool_;

    // ── Transport factory ────────────────────────────────────────────────────
    // Constructed here; injected (by reference) into CallSessionFactory so no
    // global instance() is needed (Principle 3).
    ConversationTransportFactory      transport_factory_;

    // ── Session manager ──────────────────────────────────────────────────────
    std::unique_ptr<SessionManager>   session_manager_;

    std::atomic<bool>       running_{false};
    std::atomic<bool>       shutdown_requested_{false};
    std::mutex              shutdown_mutex_;
    std::condition_variable shutdown_cv_;
};

} // namespace voiceai
