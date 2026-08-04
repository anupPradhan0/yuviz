#pragma once

#include "common/NonCopyable.h"
#include "common/NonMovable.h"

#include <functional>
#include <mutex>
#include <string>
#include <unordered_map>

namespace voiceai {

// Thread-safe uuid -> pending-transfer-resolution registry.
//
// Bridges EslEventListener's async CHANNEL_BRIDGE/CHANNEL_HANGUP events back
// to whichever CallSession is waiting on that specific call's transfer
// outcome. Deliberately decoupled from ESL protocol details (no sockets, no
// FreeSWITCH-specific parsing) so it's pure logic — trivially unit-tested,
// and reusable by anything that needs "wait for this uuid's outcome,"
// not just EslEventListener.
//
// One instance shared across the whole Gateway process (constructed in
// Application, alongside EslClient/EslEventListener/SessionManager — see
// their own single-shared-instance-per-process comments), since uuid is
// globally unique per call and events arrive on EslEventListener's single
// connection regardless of which CallSession is waiting.
class TransferCorrelator : private NonCopyable, private NonMovable {
public:
    using ResolutionHandler = std::function<void(bool success, std::string detail)>;

    TransferCorrelator() = default;

    // Registers `on_resolved` to fire exactly once for `uuid`, the next time
    // resolve() is called for it. Overwrites any existing watch for the
    // same uuid — only one transfer is ever in flight per call at a time.
    void watch(const std::string& uuid, ResolutionHandler on_resolved);

    // Removes a pending watch without firing it. Call this once something
    // else (CallFSM's own TransferTimeout) has already resolved the
    // outcome, so a late-arriving event doesn't fire a stale callback.
    void cancel(const std::string& uuid);

    // Resolves `uuid`'s pending watch, if any, and removes it — firing the
    // handler with (success, detail). Returns true if a watch was found and
    // fired; false if nothing was pending for this uuid (e.g. an unrelated
    // call's own hangup event). The handler runs synchronously on the
    // calling thread, outside the internal lock (safe for the handler to
    // call back into watch()/cancel()/resolve() itself).
    bool resolve(const std::string& uuid, bool success, std::string detail);

private:
    std::mutex                                         mutex_;
    std::unordered_map<std::string, ResolutionHandler> pending_;
};

} // namespace voiceai
