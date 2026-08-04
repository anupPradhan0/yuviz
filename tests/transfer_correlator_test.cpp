// TransferCorrelator — pure uuid -> pending-transfer-resolution logic, no
// sockets involved (see gateway/include/telephony/TransferCorrelator.h).
// EslEventListenerTest (esl_event_listener_test.cpp) covers the real
// CHANNEL_BRIDGE/CHANNEL_HANGUP-driven wiring end-to-end; this file is
// about the registry's own contract in isolation.

#include <gtest/gtest.h>

#include "telephony/TransferCorrelator.h"

using namespace voiceai;

TEST(TransferCorrelatorTest, ResolveWithNoWatchReturnsFalseAndFiresNothing) {
    TransferCorrelator correlator;
    bool fired = false;
    EXPECT_FALSE(correlator.resolve("unknown-uuid", true, "bridged"));
    EXPECT_FALSE(fired);
}

TEST(TransferCorrelatorTest, ResolveFiresTheWatchedHandlerExactlyOnce) {
    TransferCorrelator correlator;
    int fire_count = 0;
    bool last_success = false;
    std::string last_detail;

    correlator.watch("uuid-1", [&](bool success, std::string detail) {
        ++fire_count;
        last_success = success;
        last_detail  = std::move(detail);
    });

    EXPECT_TRUE(correlator.resolve("uuid-1", true, "bridged"));
    EXPECT_EQ(fire_count, 1);
    EXPECT_TRUE(last_success);
    EXPECT_EQ(last_detail, "bridged");

    // Resolving again for the same uuid must not re-fire — it was removed
    // on first resolution.
    EXPECT_FALSE(correlator.resolve("uuid-1", false, "hangup_before_bridge"));
    EXPECT_EQ(fire_count, 1);
}

TEST(TransferCorrelatorTest, ResolveCarriesFailureAndDetailThrough) {
    TransferCorrelator correlator;
    bool success = true;
    std::string detail;

    correlator.watch("uuid-2", [&](bool s, std::string d) {
        success = s;
        detail  = std::move(d);
    });

    correlator.resolve("uuid-2", false, "hangup_before_bridge");
    EXPECT_FALSE(success);
    EXPECT_EQ(detail, "hangup_before_bridge");
}

TEST(TransferCorrelatorTest, CancelRemovesWatchWithoutFiringIt) {
    TransferCorrelator correlator;
    bool fired = false;
    correlator.watch("uuid-3", [&](bool, std::string) { fired = true; });

    correlator.cancel("uuid-3");

    EXPECT_FALSE(correlator.resolve("uuid-3", true, "bridged"));
    EXPECT_FALSE(fired);
}

TEST(TransferCorrelatorTest, CancelOnUnknownUuidIsANoOp) {
    TransferCorrelator correlator;
    correlator.cancel("never-watched");  // must not throw or crash
    SUCCEED();
}

TEST(TransferCorrelatorTest, WatchOverwritesAnyExistingWatchForSameUuid) {
    TransferCorrelator correlator;
    bool first_fired  = false;
    bool second_fired = false;

    correlator.watch("uuid-4", [&](bool, std::string) { first_fired = true; });
    correlator.watch("uuid-4", [&](bool, std::string) { second_fired = true; });

    correlator.resolve("uuid-4", true, "bridged");

    EXPECT_FALSE(first_fired);
    EXPECT_TRUE(second_fired);
}

TEST(TransferCorrelatorTest, IndependentUuidsDoNotInterfere) {
    TransferCorrelator correlator;
    std::string resolved_for;

    correlator.watch("uuid-a", [&](bool, std::string) { resolved_for = "a"; });
    correlator.watch("uuid-b", [&](bool, std::string) { resolved_for = "b"; });

    correlator.resolve("uuid-b", true, "bridged");
    EXPECT_EQ(resolved_for, "b");

    correlator.resolve("uuid-a", true, "bridged");
    EXPECT_EQ(resolved_for, "a");
}

TEST(TransferCorrelatorTest, HandlerCanReenterCorrelatorWithoutDeadlock) {
    // resolve() must call the handler outside its internal lock — otherwise
    // a handler that itself calls watch()/cancel()/resolve() (plausible:
    // CallSession's real handler posts back to a control thread, but a
    // synchronous variant could legitimately re-enter) would deadlock.
    TransferCorrelator correlator;
    bool reentrant_call_succeeded = false;

    correlator.watch("uuid-5", [&](bool, std::string) {
        correlator.watch("uuid-6", [&](bool, std::string) {
            reentrant_call_succeeded = true;
        });
        correlator.resolve("uuid-6", true, "bridged");
    });

    correlator.resolve("uuid-5", true, "bridged");
    EXPECT_TRUE(reentrant_call_succeeded);
}
