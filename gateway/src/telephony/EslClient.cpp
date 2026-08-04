#include "telephony/EslClient.h"
#include "telephony/EslFraming.h"

#include <arpa/inet.h>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <chrono>

namespace voiceai {

namespace {

using esl_framing::parse_content_length;
using esl_framing::read_exact;
using esl_framing::read_until_blank_line;

bool looks_like_sip_uri(const std::string& destination) {
    return destination.rfind("sip:", 0) == 0 || destination.rfind("sips:", 0) == 0;
}

// bgapi's immediate reply carries no Content-Length body — the Job-UUID is
// a header line, e.g. "Reply-Text: +OK Job-UUID: <uuid>\nJob-UUID: <uuid>"
// (confirmed live against this deployment's FreeSWITCH — see
// docs/warm_transfer_architecture.md §6). Parses the standalone
// "Job-UUID:" header rather than the Reply-Text line, same
// header-value-extraction shape as parse_content_length() above.
bool parse_job_uuid(const std::string& headers, std::string& out_uuid) {
    const std::string key = "\nJob-UUID:";
    // Headers begin at position 0 with no leading newline — check both the
    // start-of-string and mid-string cases.
    auto pos = headers.rfind(key);
    size_t value_start;
    if (pos != std::string::npos) {
        value_start = pos + key.size();
    } else if (headers.rfind("Job-UUID:", 0) == 0) {
        value_start = std::string("Job-UUID:").size();
    } else {
        return false;
    }
    while (value_start < headers.size() && headers[value_start] == ' ') ++value_start;
    const auto end = headers.find('\n', value_start);
    out_uuid = headers.substr(value_start, end == std::string::npos
                                            ? std::string::npos : end - value_start);
    return !out_uuid.empty();
}

} // namespace

EslClient::EslClient(EslConfig cfg, Logger& logger)
    : cfg_(std::move(cfg))
    , logger_(logger)
{}

EslClient::~EslClient() {
    std::lock_guard lock{mutex_};
    disconnect_locked();
}

void EslClient::disconnect_locked() {
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
    read_buf_.clear();
}

bool EslClient::ensure_connected_locked() {
    if (fd_ >= 0) return true;

    addrinfo hints{};
    hints.ai_family   = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* res = nullptr;
    const std::string port_str = std::to_string(cfg_.port);
    if (::getaddrinfo(cfg_.host.c_str(), port_str.c_str(), &hints, &res) != 0 || !res) {
        logger_.warn("EslClient: getaddrinfo failed host={} port={}", cfg_.host, cfg_.port);
        return false;
    }

    const int fd = ::socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (fd < 0) {
        ::freeaddrinfo(res);
        logger_.warn("EslClient: socket() failed errno={}", errno);
        return false;
    }

    // Non-blocking connect with an explicit timeout — a plain connect() can
    // block far longer than acceptable against an unreachable/firewalled host.
    ::fcntl(fd, F_SETFL, O_NONBLOCK);
    const int rc = ::connect(fd, res->ai_addr, res->ai_addrlen);
    ::freeaddrinfo(res);

    if (rc < 0 && errno != EINPROGRESS) {
        logger_.warn("EslClient: connect() failed host={} port={} errno={}",
                     cfg_.host, cfg_.port, errno);
        ::close(fd);
        return false;
    }
    if (rc != 0) {
        pollfd pfd{fd, POLLOUT, 0};
        const int pr = ::poll(&pfd, 1, static_cast<int>(cfg_.connect_timeout_ms));
        int soerr = 0;
        socklen_t len = sizeof(soerr);
        if (pr <= 0 || ::getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &len) != 0 || soerr != 0) {
            logger_.warn("EslClient: connect timed out/failed host={} port={}",
                         cfg_.host, cfg_.port);
            ::close(fd);
            return false;
        }
    }
    // Back to blocking mode — the read/write helpers below bound their own
    // timeouts via poll(), so blocking mode here just keeps send() simple.
    const int flags = ::fcntl(fd, F_GETFL, 0);
    ::fcntl(fd, F_SETFL, flags & ~O_NONBLOCK);

    const auto timeout = std::chrono::milliseconds{cfg_.connect_timeout_ms};
    std::string carry, headers;
    if (!read_until_blank_line(fd, carry, headers, timeout) ||
        headers.find("auth/request") == std::string::npos) {
        logger_.warn("EslClient: did not receive auth/request from {}:{}", cfg_.host, cfg_.port);
        ::close(fd);
        return false;
    }

    const std::string auth_cmd = "auth " + cfg_.password + "\n\n";
    if (::send(fd, auth_cmd.data(), auth_cmd.size(), 0) < 0) {
        logger_.warn("EslClient: send(auth) failed errno={}", errno);
        ::close(fd);
        return false;
    }

    std::string auth_reply;
    if (!read_until_blank_line(fd, carry, auth_reply, timeout) ||
        auth_reply.find("+OK") == std::string::npos) {
        logger_.warn("EslClient: auth rejected by {}:{} (check esl.password in gateway.yaml)",
                     cfg_.host, cfg_.port);
        ::close(fd);
        return false;
    }

    fd_ = fd;
    read_buf_ = std::move(carry);   // any bytes read past the auth reply (normally none)
    logger_.info("EslClient: connected and authenticated host={} port={}", cfg_.host, cfg_.port);
    return true;
}

bool EslClient::send_command_locked(const std::string& command, std::string& reply_out) {
    if (!ensure_connected_locked()) return false;

    const std::string full = command + "\n\n";
    if (::send(fd_, full.data(), full.size(), 0) < 0) {
        logger_.warn("EslClient: send() failed errno={} command={}", errno, command);
        disconnect_locked();
        return false;
    }

    const auto timeout = std::chrono::milliseconds{cfg_.connect_timeout_ms};
    std::string headers;
    if (!read_until_blank_line(fd_, read_buf_, headers, timeout)) {
        logger_.warn("EslClient: no reply headers command={}", command);
        disconnect_locked();
        return false;
    }

    const size_t content_length = parse_content_length(headers);
    if (content_length == 0) {
        reply_out = headers;   // command/reply carries its result in Reply-Text
        return true;
    }

    std::string body;
    if (!read_exact(fd_, read_buf_, body, content_length, timeout)) {
        logger_.warn("EslClient: incomplete reply body command={}", command);
        disconnect_locked();
        return false;
    }
    reply_out = body;   // api/response carries its result in the body
    return true;
}

void EslClient::hangup(const std::string& uuid, const std::string& reason) {
    if (!cfg_.enabled) {
        logger_.info("EslClient: disabled (esl.enabled=false) — not hanging up SIP leg "
                     "uuid={} reason={}; WebSocket/audio already closed", uuid, reason);
        return;
    }
    if (uuid.empty()) {
        logger_.warn("EslClient: hangup requested with empty call_id — skipping "
                     "(mod_audio_fork's \"start\" callSid may not have arrived yet) reason={}",
                     reason);
        return;
    }

    std::lock_guard lock{mutex_};
    std::string reply;
    if (!send_command_locked("api uuid_kill " + uuid, reply)) {
        logger_.warn("EslClient: hangup failed uuid={} reason={} "
                     "(ESL unreachable or unauthenticated — call remains connected)",
                     uuid, reason);
        return;
    }
    if (reply.find("+OK") != std::string::npos) {
        logger_.info("EslClient: hangup issued uuid={} reason={}", uuid, reason);
    } else {
        logger_.warn("EslClient: hangup command rejected uuid={} reason={} reply={}",
                     uuid, reason, reply);
    }
}

bool EslClient::transfer(const TransferRequest& req, std::string& error_out) {
    const std::string& uuid        = req.call_id;
    const std::string& destination = req.destination;
    const std::string& reason      = req.reason;
    const std::string& transfer_id = req.transfer_id;

    if (!cfg_.enabled) {
        error_out = "esl_disabled";
        logger_.info("EslClient: disabled (esl.enabled=false) — not transferring "
                     "uuid={} destination={} reason={}", uuid, destination, reason);
        return false;
    }
    if (uuid.empty()) {
        error_out = "empty_uuid";
        logger_.warn("EslClient: transfer requested with empty call_id — skipping "
                     "destination={} reason={}", destination, reason);
        return false;
    }
    if (destination.empty()) {
        error_out = "empty_destination";
        logger_.warn("EslClient: transfer requested with empty destination uuid={} reason={}",
                     uuid, reason);
        return false;
    }

    // Phone number/extension: normal XML dialplan extension lookup.
    // SIP URI: "inline" dialplan lets the destination be a direct app:data
    // string instead of an extension name — bridge straight to the URI via
    // mod_sofia's external profile, with no dialplan entry required for it.
    const std::string command = looks_like_sip_uri(destination)
        ? "api uuid_transfer " + uuid + " 'bridge:sofia/external/" + destination + "' inline"
        : "api uuid_transfer " + uuid + " " + destination + " XML default";

    std::lock_guard lock{mutex_};
    std::string reply;
    if (!send_command_locked(command, reply)) {
        error_out = "esl_unreachable";
        logger_.warn("EslClient: transfer failed uuid={} destination={} reason={} "
                     "(ESL unreachable or unauthenticated — call remains connected)",
                     uuid, destination, reason);
        return false;
    }
    if (reply.find("+OK") != std::string::npos) {
        // "Accepted", not "succeeded" — see the doc comment on transfer()
        // in EslClient.h. The real outcome is resolved later via
        // TransferCorrelator from a CHANNEL_BRIDGE/CHANNEL_HANGUP event.
        logger_.info("EslClient: transfer command accepted uuid={} destination={} reason={} "
                     "transfer_id={} — awaiting CHANNEL_BRIDGE/CHANNEL_HANGUP to confirm outcome",
                     uuid, destination, reason, transfer_id);
        return true;
    }
    error_out = reply;
    logger_.warn("EslClient: transfer command rejected uuid={} destination={} reason={} reply={}",
                 uuid, destination, reason, reply);
    return false;
}

bool EslClient::originate_async(const std::string& destination,
                                const std::string& caller_id_number,
                                std::string& out_job_uuid, std::string& error_out) {
    if (!cfg_.enabled) {
        error_out = "esl_disabled";
        logger_.info("EslClient: disabled (esl.enabled=false) — not originating "
                     "destination={}", destination);
        return false;
    }
    if (destination.empty()) {
        error_out = "empty_destination";
        logger_.warn("EslClient: originate requested with empty destination");
        return false;
    }

    // SIP URI: dial directly. Plain extension/number: dial directly against
    // the deployment's SIP proxy (cfg_.sip_proxy_host/port — Kamailio in
    // this deployment, see docs/warm_transfer_architecture.md §6), which
    // owns the real registrar and resolves/forks to the extension's actual
    // contact(s) itself.
    //
    // Two rejected alternatives, in order:
    //   1. loopback/<dest>/default (reuse the "default" dialplan context's
    //      own extension-to-extension routing): a loopback A-leg's &park()
    //      begins running as soon as the loopback pairing itself connects —
    //      NOT when the real destination actually answers — so
    //      BACKGROUND_JOB reported false "success" while the B-leg was
    //      still independently trying (and possibly failing) to reach the
    //      real destination. WarmTransferCoordinator was already bridging
    //      the customer to a dead loopback leg before the real failure
    //      surfaced.
    //   2. FreeSWITCH's "user/<id>" channel type (resolves via FreeSWITCH's
    //      own directory/registration): correct only when FreeSWITCH itself
    //      is the SIP registrar. In any deployment fronted by a SIP proxy —
    //      this one included — real phones register with the proxy, not
    //      FreeSWITCH, so "user/<id>" fails with USER_NOT_REGISTERED even
    //      when the destination is genuinely online (confirmed live).
    //
    // Dialing the proxy directly avoids both: no loopback pairing to
    // decouple &park()'s timing from the real answer, and no dependency on
    // FreeSWITCH's own directory knowing about the extension at all — the
    // proxy's registrar handles that, exactly as it already does for every
    // other call in this deployment.
    const std::string dial_string = looks_like_sip_uri(destination)
        ? "sofia/external/" + destination
        : "sofia/external/sip:" + destination + "@" + cfg_.sip_proxy_host + ":" +
              std::to_string(cfg_.sip_proxy_port);

    const std::string command = "bgapi originate {origination_caller_id_number=" +
        caller_id_number + "}" + dial_string + " &park()";

    std::lock_guard lock{mutex_};
    std::string reply;
    if (!send_command_locked(command, reply)) {
        error_out = "esl_unreachable";
        logger_.warn("EslClient: originate failed destination={} "
                     "(ESL unreachable or unauthenticated)", destination);
        return false;
    }
    if (reply.find("+OK") == std::string::npos || !parse_job_uuid(reply, out_job_uuid)) {
        error_out = reply;
        logger_.warn("EslClient: originate command rejected destination={} reply={}",
                     destination, reply);
        return false;
    }
    logger_.info("EslClient: originate accepted destination={} job_uuid={} — "
                 "awaiting BACKGROUND_JOB/CHANNEL_ANSWER to confirm outcome",
                 destination, out_job_uuid);
    return true;
}

bool EslClient::bridge(const std::string& uuid_a, const std::string& uuid_b,
                       std::string& error_out) {
    if (!cfg_.enabled) {
        error_out = "esl_disabled";
        return false;
    }
    if (uuid_a.empty() || uuid_b.empty()) {
        error_out = "empty_uuid";
        logger_.warn("EslClient: bridge requested with an empty uuid uuid_a={} uuid_b={}",
                     uuid_a, uuid_b);
        return false;
    }

    std::lock_guard lock{mutex_};
    std::string reply;
    if (!send_command_locked("api uuid_bridge " + uuid_a + " " + uuid_b, reply)) {
        error_out = "esl_unreachable";
        logger_.warn("EslClient: bridge failed uuid_a={} uuid_b={} "
                     "(ESL unreachable or unauthenticated)", uuid_a, uuid_b);
        return false;
    }
    if (reply.find("+OK") != std::string::npos) {
        logger_.info("EslClient: bridge command accepted uuid_a={} uuid_b={}", uuid_a, uuid_b);
        return true;
    }
    error_out = reply;
    logger_.warn("EslClient: bridge command rejected uuid_a={} uuid_b={} reply={}",
                 uuid_a, uuid_b, reply);
    return false;
}

bool EslClient::stop_audio_fork(const std::string& uuid, std::string& error_out) {
    if (!cfg_.enabled) {
        error_out = "esl_disabled";
        return false;
    }
    if (uuid.empty()) {
        error_out = "empty_uuid";
        return false;
    }

    std::lock_guard lock{mutex_};
    std::string reply;
    if (!send_command_locked("api uuid_audio_fork " + uuid + " stop", reply)) {
        error_out = "esl_unreachable";
        logger_.warn("EslClient: stop_audio_fork failed uuid={} "
                     "(ESL unreachable or unauthenticated)", uuid);
        return false;
    }
    if (reply.find("+OK") != std::string::npos) {
        logger_.info("EslClient: audio fork stopped uuid={}", uuid);
        return true;
    }
    error_out = reply;
    logger_.warn("EslClient: stop_audio_fork rejected uuid={} reply={}", uuid, reply);
    return false;
}

bool EslClient::hold(const std::string& uuid, std::string& error_out) {
    if (!cfg_.enabled) {
        error_out = "esl_disabled";
        return false;
    }
    if (uuid.empty()) {
        error_out = "empty_uuid";
        return false;
    }

    std::lock_guard lock{mutex_};
    std::string reply;
    if (!send_command_locked("api uuid_hold " + uuid, reply)) {
        error_out = "esl_unreachable";
        return false;
    }
    if (reply.find("+OK") != std::string::npos) return true;
    error_out = reply;
    logger_.warn("EslClient: hold rejected uuid={} reply={}", uuid, reply);
    return false;
}

bool EslClient::unhold(const std::string& uuid, std::string& error_out) {
    if (!cfg_.enabled) {
        error_out = "esl_disabled";
        return false;
    }
    if (uuid.empty()) {
        error_out = "empty_uuid";
        return false;
    }

    std::lock_guard lock{mutex_};
    std::string reply;
    if (!send_command_locked("api uuid_hold off " + uuid, reply)) {
        error_out = "esl_unreachable";
        return false;
    }
    if (reply.find("+OK") != std::string::npos) return true;
    error_out = reply;
    logger_.warn("EslClient: unhold rejected uuid={} reply={}", uuid, reply);
    return false;
}

} // namespace voiceai
