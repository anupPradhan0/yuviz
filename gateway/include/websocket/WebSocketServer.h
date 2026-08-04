#pragma once

#include "websocket/IWebSocketServer.h"
#include "websocket/WebSocketConnection.h"
#include "config/Config.h"
#include "logging/Logger.h"
#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include <atomic>
#include <memory>
#include <mutex>
#include <thread>
#include <unordered_map>
#include <libwebsockets.h>

namespace voiceai {

class WebSocketServer final : public IWebSocketServer, private NonCopyable, private NonMovable {
public:
    WebSocketServer(const WebSocketConfig& config, Logger& logger);
    ~WebSocketServer() override;

    bool initialize() override;
    bool start()      override;
    void stop()       override;
    void shutdown()   override;

    [[nodiscard]] bool is_running() const noexcept override { return running_.load(); }

    void set_on_connect(ConnectHandler handler)       override;
    void set_on_disconnect(DisconnectHandler handler) override;

private:
    static int lws_callback(struct lws*              wsi,
                            enum lws_callback_reasons reason,
                            void*                    user,
                            void*                    in,
                            size_t                   len);

    void service_loop();

    std::shared_ptr<WebSocketConnection> find_connection(struct lws* wsi) const;
    void add_connection(struct lws* wsi, std::shared_ptr<WebSocketConnection> conn);
    void remove_connection(struct lws* wsi);

    WebSocketConfig     config_;
    Logger&             logger_;
    struct lws_context* context_{nullptr};
    std::thread         service_thread_;
    std::atomic<bool>   running_{false};

    ConnectHandler    on_connect_;
    DisconnectHandler on_disconnect_;

    mutable std::mutex                                                    conn_mutex_;
    std::unordered_map<struct lws*, std::shared_ptr<WebSocketConnection>> connections_;
};

} // namespace voiceai
