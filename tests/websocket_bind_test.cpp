// The Gateway's audio WebSocket is unauthenticated, so it must bind only the
// configured host: loopback by default (GATEWAY_LISTEN_HOST).

#include "config/Config.h"
#include "logging/Logger.h"
#include "websocket/WebSocketServer.h"

#include <gtest/gtest.h>

#include <arpa/inet.h>
#include <ifaddrs.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <string>

namespace {

uint16_t free_port() {
    const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in addr{};
    addr.sin_family      = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    ::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    socklen_t len = sizeof(addr);
    ::getsockname(fd, reinterpret_cast<sockaddr*>(&addr), &len);
    ::close(fd);
    return ntohs(addr.sin_port);
}

std::string first_non_loopback_ipv4() {
    ifaddrs* list = nullptr;
    if (::getifaddrs(&list) != 0) return {};
    std::string found;
    for (ifaddrs* it = list; it != nullptr && found.empty(); it = it->ifa_next) {
        if (it->ifa_addr == nullptr || it->ifa_addr->sa_family != AF_INET) continue;
        const auto* in = reinterpret_cast<sockaddr_in*>(it->ifa_addr);
        if (ntohl(in->sin_addr.s_addr) >> 24 == 127) continue;
        char buf[INET_ADDRSTRLEN];
        ::inet_ntop(AF_INET, &in->sin_addr, buf, sizeof(buf));
        found = buf;
    }
    ::freeifaddrs(list);
    return found;
}

bool can_connect(const std::string& ip, uint16_t port) {
    const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port   = htons(port);
    ::inet_pton(AF_INET, ip.c_str(), &addr.sin_addr);
    const bool ok = ::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == 0;
    ::close(fd);
    return ok;
}

}  // namespace

TEST(WebSocketBindTest, LoopbackHostIsNotReachableFromOtherInterfaces) {
    const std::string lan_ip = first_non_loopback_ipv4();
    if (lan_ip.empty()) GTEST_SKIP() << "no non-loopback IPv4 interface on this machine";

    voiceai::WebSocketConfig cfg;
    cfg.host = "127.0.0.1";
    cfg.port = free_port();
    voiceai::Logger logger = voiceai::Logger::make_null();
    voiceai::WebSocketServer server{cfg, logger};
    ASSERT_TRUE(server.start());

    EXPECT_TRUE(can_connect("127.0.0.1", cfg.port));
    EXPECT_FALSE(can_connect(lan_ip, cfg.port)) << "reachable on " << lan_ip;
    server.stop();
}
