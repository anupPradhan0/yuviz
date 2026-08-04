#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>

namespace voiceai {

class IWebSocketConnection {
public:
    using BinaryHandler = std::function<void(const uint8_t*, size_t)>;
    using TextHandler   = std::function<void(const std::string&)>;
    using CloseHandler  = std::function<void()>;

    virtual ~IWebSocketConnection() = default;

    [[nodiscard]] virtual const std::string& id()      const noexcept = 0;
    [[nodiscard]] virtual const std::string& path()    const noexcept = 0;
    [[nodiscard]] virtual bool               is_open() const noexcept = 0;

    virtual void send_text(const std::string& msg)           = 0;
    virtual void send_binary(const uint8_t* data, size_t n)  = 0;
    virtual void close()                                      = 0;

    virtual void set_on_binary(BinaryHandler handler) = 0;
    virtual void set_on_text(TextHandler handler)     = 0;
    virtual void set_on_close(CloseHandler handler)   = 0;
};

} // namespace voiceai
