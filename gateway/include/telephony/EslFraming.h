#pragma once

// Shared low-level framing helpers for FreeSWITCH's ESL plain-text
// protocol, used by both EslClient (fixed-timeout command/response reads)
// and EslEventListener (indefinite event-wait reads via
// wait_for_frame_header, which has its own waiting discipline and stays
// local to EslEventListener.cpp — see that file's own comment). These
// three are the ones with byte-identical bodies across both call sites;
// consolidated here rather than duplicated, since none of this is
// per-audio-frame code (it's one-time ESL command/event I/O), so sharing
// it costs nothing on the call's hot path.

#include <chrono>
#include <cstdlib>
#include <poll.h>
#include <string>
#include <sys/socket.h>

namespace voiceai::esl_framing {

// Read from `fd` until `carry` contains a blank-line terminator ("\n\n",
// which also matches inside "\r\n\r\n"), or `timeout` elapses. On success,
// `out` holds everything up to (not including) the terminator, and any
// bytes read past it are left in `carry` for the next call.
inline bool read_until_blank_line(int fd, std::string& carry, std::string& out,
                                   std::chrono::milliseconds timeout) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    for (;;) {
        const auto pos = carry.find("\n\n");
        if (pos != std::string::npos) {
            out = carry.substr(0, pos);
            carry.erase(0, pos + 2);
            return true;
        }
        const auto remaining = deadline - std::chrono::steady_clock::now();
        if (remaining <= std::chrono::milliseconds{0}) return false;

        pollfd pfd{fd, POLLIN, 0};
        const int rc = ::poll(&pfd, 1, static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(remaining).count()));
        if (rc <= 0) return false;   // timeout or poll error

        char buf[4096];
        const ssize_t n = ::recv(fd, buf, sizeof(buf), 0);
        if (n <= 0) return false;    // peer closed or error
        carry.append(buf, static_cast<size_t>(n));
    }
}

// Read exactly `n` bytes (an ESL api/response body, per its Content-Length).
inline bool read_exact(int fd, std::string& carry, std::string& out, size_t n,
                       std::chrono::milliseconds timeout) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (carry.size() < n) {
        const auto remaining = deadline - std::chrono::steady_clock::now();
        if (remaining <= std::chrono::milliseconds{0}) return false;

        pollfd pfd{fd, POLLIN, 0};
        const int rc = ::poll(&pfd, 1, static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(remaining).count()));
        if (rc <= 0) return false;

        char buf[4096];
        const ssize_t got = ::recv(fd, buf, sizeof(buf), 0);
        if (got <= 0) return false;
        carry.append(buf, static_cast<size_t>(got));
    }
    out = carry.substr(0, n);
    carry.erase(0, n);
    return true;
}

inline size_t parse_content_length(const std::string& headers) {
    const std::string key = "Content-Length:";
    const auto pos = headers.find(key);
    if (pos == std::string::npos) return 0;
    auto start = pos + key.size();
    while (start < headers.size() && headers[start] == ' ') ++start;
    return static_cast<size_t>(std::strtoul(headers.c_str() + start, nullptr, 10));
}

} // namespace voiceai::esl_framing
