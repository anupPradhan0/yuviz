// Phase 5 of AI-to-human transfer: EslClient::transfer() (uuid_transfer).
//
// EslClient speaks ESL's plain-text protocol over a raw TCP socket (see
// gateway/src/telephony/EslClient.cpp) — there is no fake/mock ESL server
// library to inject, so this test runs a minimal real one on 127.0.0.1 (a
// background thread speaking just enough of the protocol: auth/request,
// auth accept/reject, and api command/response framing) and asserts against
// the exact commands EslClient sends and how it interprets replies.

#include <gtest/gtest.h>

#include "config/Config.h"
#include "logging/Logger.h"
#include "telephony/EslClient.h"
#include "telephony/TransferRequest.h"

#include <arpa/inet.h>
#include <atomic>
#include <netinet/in.h>
#include <string>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace {

using namespace voiceai;
using namespace std::chrono_literals;

// Reads until a blank-line ("\n\n") terminator; returns everything before it.
std::string recv_until_blank_line(int fd) {
    std::string buf;
    char chunk[4096];
    while (buf.find("\n\n") == std::string::npos) {
        const ssize_t n = ::recv(fd, chunk, sizeof(chunk), 0);
        if (n <= 0) break;
        buf.append(chunk, static_cast<size_t>(n));
    }
    const auto pos = buf.find("\n\n");
    return pos == std::string::npos ? buf : buf.substr(0, pos);
}

void send_all(int fd, const std::string& data) {
    ::send(fd, data.data(), data.size(), 0);
}

// A single scripted exchange: after auth succeeds, the server expects one
// command and sends back the given api/response body (wrapped with the
// correct Content-Length framing) — or, if `reject_auth` is set on the
// fixture, never gets this far at all.
struct FakeEslServer {
    int listen_fd{-1};
    uint16_t port{0};
    std::thread server_thread;
    std::atomic<bool> stop{false};

    std::string expected_password = "ClueCon";
    bool        reject_auth        = false;
    bool        close_before_reply = false;  // simulate ESL unreachable mid-command
    std::string next_reply_body    = "+OK";  // body of the api/response to the next command

    std::vector<std::string> received_commands;  // every "api ..." command seen, in order

    void start() {
        listen_fd = ::socket(AF_INET, SOCK_STREAM, 0);
        int opt = 1;
        ::setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

        sockaddr_in addr{};
        addr.sin_family      = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        addr.sin_port        = 0;  // ephemeral
        ::bind(listen_fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));

        sockaddr_in bound{};
        socklen_t   len = sizeof(bound);
        ::getsockname(listen_fd, reinterpret_cast<sockaddr*>(&bound), &len);
        port = ntohs(bound.sin_port);

        ::listen(listen_fd, 4);

        server_thread = std::thread([this] { run(); });
    }

    void run() {
        while (!stop.load()) {
            sockaddr_in peer{};
            socklen_t   plen = sizeof(peer);
            const int   fd   = ::accept(listen_fd, reinterpret_cast<sockaddr*>(&peer), &plen);
            if (fd < 0) return;  // listen_fd closed → stop()

            send_all(fd, "Content-Type: auth/request\n\n");
            const std::string auth_line = recv_until_blank_line(fd);

            const bool ok = !reject_auth && auth_line == ("auth " + expected_password);
            send_all(fd, ok
                ? "Content-Type: command/reply\nReply-Text: +OK accepted\n\n"
                : "Content-Type: command/reply\nReply-Text: -ERR invalid\n\n");
            if (!ok) { ::close(fd); continue; }

            // One command per connection is all this test needs.
            const std::string command = recv_until_blank_line(fd);
            received_commands.push_back(command);

            if (close_before_reply) { ::close(fd); continue; }

            const std::string body = "Content-Type: api/response\nContent-Length: "
                + std::to_string(next_reply_body.size()) + "\n\n" + next_reply_body;
            send_all(fd, body);
            ::close(fd);
        }
    }

    void stop_server() {
        stop.store(true);
        if (listen_fd >= 0) ::shutdown(listen_fd, SHUT_RDWR);
        if (listen_fd >= 0) ::close(listen_fd);
        if (server_thread.joinable()) server_thread.join();
    }

    ~FakeEslServer() { stop_server(); }
};

TransferRequest make_req(std::string call_id, std::string destination, std::string reason = "x") {
    return TransferRequest{std::move(call_id), "cold", std::move(destination), std::move(reason)};
}

EslConfig make_cfg(uint16_t port) {
    EslConfig cfg;
    cfg.enabled            = true;
    cfg.host               = "127.0.0.1";
    cfg.port               = port;
    cfg.password           = "ClueCon";
    cfg.connect_timeout_ms = 500;
    return cfg;
}

} // namespace

// ── uuid_transfer command construction ──────────────────────────────────────

TEST(EslClientTransferTest, PhoneNumberDestinationUsesXmlDefaultDialplan) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "+OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-1", "1005", "customer_requested"), error);

    EXPECT_TRUE(ok);
    EXPECT_TRUE(error.empty());
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0], "api uuid_transfer call-uuid-1 1005 XML default");
}

TEST(EslClientTransferTest, SipUriDestinationUsesInlineBridge) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "+OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-2", "sip:agent@example.com",
                                                  "escalation_threshold_exceeded"), error);

    EXPECT_TRUE(ok);
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0],
              "api uuid_transfer call-uuid-2 'bridge:sofia/external/sip:agent@example.com' inline");
}

TEST(EslClientTransferTest, SipsUriAlsoDetectedAsSipUri) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "+OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    client.transfer(make_req("call-uuid-3", "sips:agent@example.com"), error);

    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_NE(server.received_commands[0].find("bridge:sofia/external/sips:agent@example.com"),
              std::string::npos);
}

// ── Reply interpretation ─────────────────────────────────────────────────────

TEST(EslClientTransferTest, RejectedTransferReturnsFalseWithReplyAsError) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "-ERR NO_ROUTE_DESTINATION";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-4", "9999999"), error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "-ERR NO_ROUTE_DESTINATION");
}

// ── Guard clauses (no network round trip expected) ──────────────────────────

TEST(EslClientTransferTest, DisabledConfigReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);  // port 1: nothing listens there
    cfg.enabled = false;
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-5", "1005"), error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "esl_disabled");
}

TEST(EslClientTransferTest, EmptyUuidReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string error;
    const bool ok = client.transfer(make_req("", "1005"), error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "empty_uuid");
}

TEST(EslClientTransferTest, EmptyDestinationReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-6", ""), error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "empty_destination");
}

// ── Connection failure ───────────────────────────────────────────────────────

TEST(EslClientTransferTest, UnreachableEslReturnsFalse) {
    // Nothing listens on this port; connect() itself fails/times out fast
    // via EslClient's own non-blocking-connect-with-timeout path.
    EslConfig cfg = make_cfg(1);
    cfg.connect_timeout_ms = 100;
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-7", "1005"), error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "esl_unreachable");
}

TEST(EslClientTransferTest, AuthRejectedReturnsFalse) {
    FakeEslServer server;
    server.reject_auth = true;
    server.start();

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-8", "1005"), error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "esl_unreachable");
    EXPECT_TRUE(server.received_commands.empty());  // never got past auth
}

TEST(EslClientTransferTest, ConnectionDroppedMidCommandReturnsFalse) {
    FakeEslServer server;
    server.close_before_reply = true;
    server.start();

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-9", "1005"), error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "esl_unreachable");
    ASSERT_EQ(server.received_commands.size(), 1u);  // command was sent before the drop
}

// ── hangup() — same request/reply shape, previously untested ────────────────

TEST(EslClientHangupTest, SuccessfulHangupIssuesUuidKill) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "+OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    client.hangup("call-uuid-10", "caller_hangup");

    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0], "api uuid_kill call-uuid-10");
}

TEST(EslClientHangupTest, EmptyUuidSkipsConnectionEntirely) {
    EslConfig cfg = make_cfg(1);
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    // Must not throw and must not hang waiting on a nonexistent server.
    client.hangup("", "caller_hangup");
}

// ── originate_async() / bridge() / stop_audio_fork() / hold() / unhold() ───
// Warm transfer's 5 new commands (see docs/warm_transfer_architecture.md
// §6). bgapi's own immediate reply carries no Content-Length body — the
// Job-UUID is a header line — so FakeEslServer's next_reply_body here is
// itself header text, matching send_command_locked's content_length==0
// ("command/reply carries its result in Reply-Text") branch.

TEST(EslClientOriginateAsyncTest, PlainExtensionDialsThroughSipProxyDirectly) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: +OK Job-UUID: job-abc-123\nJob-UUID: job-abc-123";

    Logger logger = Logger::make_null();
    EslConfig cfg = make_cfg(server.port);
    cfg.sip_proxy_host = "192.168.0.116";
    cfg.sip_proxy_port = 5060;
    EslClient client{cfg, logger};

    std::string job_uuid, error;
    const bool ok = client.originate_async("1001", "+15551234567", job_uuid, error);

    EXPECT_TRUE(ok);
    EXPECT_EQ(job_uuid, "job-abc-123");
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0],
              "bgapi originate {origination_caller_id_number=+15551234567}"
              "sofia/external/sip:1001@192.168.0.116:5060 &park()");
}

TEST(EslClientOriginateAsyncTest, SipUriDestinationDialsDirectly) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: +OK Job-UUID: job-xyz\nJob-UUID: job-xyz";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string job_uuid, error;
    client.originate_async("sip:agent@example.com", "+15551234567", job_uuid, error);

    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0],
              "bgapi originate {origination_caller_id_number=+15551234567}"
              "sofia/external/sip:agent@example.com &park()");
}

TEST(EslClientOriginateAsyncTest, RejectedOriginateReturnsFalseWithReplyAsError) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: -ERR DESTINATION_OUT_OF_ORDER";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string job_uuid, error;
    const bool ok = client.originate_async("1001", "+15551234567", job_uuid, error);

    EXPECT_FALSE(ok);
    EXPECT_TRUE(job_uuid.empty());
    EXPECT_EQ(error, "Reply-Text: -ERR DESTINATION_OUT_OF_ORDER");
}

TEST(EslClientOriginateAsyncTest, DisabledConfigReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);
    cfg.enabled = false;
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string job_uuid, error;
    const bool ok = client.originate_async("1001", "+15551234567", job_uuid, error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "esl_disabled");
}

TEST(EslClientOriginateAsyncTest, EmptyDestinationReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string job_uuid, error;
    const bool ok = client.originate_async("", "+15551234567", job_uuid, error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "empty_destination");
}

TEST(EslClientBridgeTest, SuccessfulBridgeIssuesUuidBridge) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: +OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.bridge("customer-uuid", "agent-uuid", error);

    EXPECT_TRUE(ok);
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0], "api uuid_bridge customer-uuid agent-uuid");
}

TEST(EslClientBridgeTest, RejectedBridgeReturnsFalse) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: -ERR NO_ANSWER";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.bridge("customer-uuid", "agent-uuid", error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "Reply-Text: -ERR NO_ANSWER");
}

TEST(EslClientBridgeTest, EmptyUuidReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string error;
    EXPECT_FALSE(client.bridge("", "agent-uuid", error));
    EXPECT_EQ(error, "empty_uuid");
    EXPECT_FALSE(client.bridge("customer-uuid", "", error));
    EXPECT_EQ(error, "empty_uuid");
}

TEST(EslClientStopAudioForkTest, SuccessfulStopIssuesUuidAudioForkStop) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: +OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.stop_audio_fork("customer-uuid", error);

    EXPECT_TRUE(ok);
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0], "api uuid_audio_fork customer-uuid stop");
}

TEST(EslClientStopAudioForkTest, EmptyUuidReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string error;
    EXPECT_FALSE(client.stop_audio_fork("", error));
    EXPECT_EQ(error, "empty_uuid");
}

TEST(EslClientHoldTest, SuccessfulHoldIssuesUuidHold) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: +OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.hold("customer-uuid", error);

    EXPECT_TRUE(ok);
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0], "api uuid_hold customer-uuid");
}

TEST(EslClientHoldTest, RejectedHoldReturnsFalse) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: -ERR NO_SUCH_CHANNEL";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    EXPECT_FALSE(client.hold("customer-uuid", error));
    EXPECT_EQ(error, "Reply-Text: -ERR NO_SUCH_CHANNEL");
}

TEST(EslClientUnholdTest, SuccessfulUnholdIssuesUuidHoldOff) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: +OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.unhold("customer-uuid", error);

    EXPECT_TRUE(ok);
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0], "api uuid_hold off customer-uuid");
}

TEST(EslClientUnholdTest, EmptyUuidReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string error;
    EXPECT_FALSE(client.unhold("", error));
    EXPECT_EQ(error, "empty_uuid");
}
