#pragma once

#include "core/IComponent.h"
#include "websocket/IWebSocketConnection.h"
#include <functional>
#include <memory>
#include <string>

namespace voiceai {

class IWebSocketServer : public IComponent {
public:
    using ConnectHandler    = std::function<void(std::shared_ptr<IWebSocketConnection>)>;
    using DisconnectHandler = std::function<void(const std::string& connection_id)>;

    virtual ~IWebSocketServer() = default;

    // IComponent provides: bool initialize(), bool start(), void stop(), void shutdown()

    [[nodiscard]] virtual bool is_running() const noexcept = 0;

    virtual void set_on_connect(ConnectHandler handler)       = 0;
    virtual void set_on_disconnect(DisconnectHandler handler) = 0;
};

} // namespace voiceai
