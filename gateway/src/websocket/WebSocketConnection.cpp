#include "websocket/WebSocketConnection.h"
#include <cstring>

namespace voiceai {

WebSocketConnection::WebSocketConnection(struct lws* wsi, std::string id, std::string path)
    : wsi_(wsi)
    , id_(std::move(id))
    , path_(std::move(path))
{}

WebSocketConnection::~WebSocketConnection() {
    handle_close();
}

void WebSocketConnection::send_text(const std::string& msg) {
    std::vector<uint8_t> buf(LWS_PRE + msg.size());
    std::memcpy(buf.data() + LWS_PRE, msg.data(), msg.size());
    enqueue(std::move(buf), LWS_WRITE_TEXT);
}

void WebSocketConnection::send_binary(const uint8_t* data, size_t n) {
    std::vector<uint8_t> buf(LWS_PRE + n);
    std::memcpy(buf.data() + LWS_PRE, data, n);
    enqueue(std::move(buf), LWS_WRITE_BINARY);
}

void WebSocketConnection::close() {
    open_.store(false);
    // Read wsi_ under write_mutex_ so we don't race with handle_close().
    // Use lws_cancel_service() — unlike lws_callback_on_writable() it is
    // documented as safe to call from any thread.
    struct lws* w;
    {
        std::lock_guard lock{write_mutex_};
        w = wsi_;
    }
    if (w) lws_cancel_service(lws_get_context(w));
}

void WebSocketConnection::set_on_binary(BinaryHandler handler) {
    on_binary_ = std::move(handler);
}

void WebSocketConnection::set_on_text(TextHandler handler) {
    on_text_ = std::move(handler);
}

void WebSocketConnection::set_on_close(CloseHandler handler) {
    on_close_ = std::move(handler);
}

void WebSocketConnection::handle_receive(const uint8_t* data, size_t n, bool is_binary) {
    if (!open_.load()) return;
    if (is_binary) {
        if (on_binary_) on_binary_(data, n);
    } else {
        if (on_text_) on_text_(std::string(reinterpret_cast<const char*>(data), n));
    }
}

void WebSocketConnection::handle_close() {
    if (!open_.exchange(false)) return;
    // Null wsi_ under write_mutex_ so enqueue() on the drain thread never
    // reads a non-null pointer that is about to be invalidated by lws.
    {
        std::lock_guard lock{write_mutex_};
        wsi_ = nullptr;
    }
    if (on_close_) on_close_();
}

void WebSocketConnection::flush_pending_writes() {
    std::lock_guard lock{write_mutex_};
    if (!wsi_) {
        // Connection already closed — discard pending writes.
        while (!write_queue_.empty()) write_queue_.pop();
        return;
    }
    while (!write_queue_.empty()) {
        auto& pw = write_queue_.front();
        const size_t payload = pw.buf.size() - LWS_PRE;
        const int rc = lws_write(wsi_, pw.buf.data() + LWS_PRE, payload, pw.proto);
        write_queue_.pop();
        if (rc < 0) {
            // lws_write() error: socket is broken.  Discard remaining writes;
            // lws will fire LWS_CALLBACK_CLOSED which calls handle_close().
            while (!write_queue_.empty()) write_queue_.pop();
            wsi_ = nullptr;
            break;
        }
    }
}

bool WebSocketConnection::has_pending_writes() const noexcept {
    std::lock_guard lock{write_mutex_};
    return !write_queue_.empty();
}

void WebSocketConnection::enqueue(std::vector<uint8_t> buf_with_pre, lws_write_protocol proto) {
    struct lws* w;
    {
        std::lock_guard lock{write_mutex_};
        write_queue_.push({std::move(buf_with_pre), proto});
        // Capture wsi_ under the lock so we never dereference it after
        // handle_close() has nulled it on the lws service thread.
        w = wsi_;
    }
    // lws_cancel_service() is thread-safe; lws_callback_on_writable() is NOT.
    if (w) lws_cancel_service(lws_get_context(w));
}

} // namespace voiceai
