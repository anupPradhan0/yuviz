#pragma once

#include "core/IComponent.h"
#include "events/SessionEvent.h"

#include <cstdint>
#include <functional>

namespace voiceai {

// Pure event bus.  Producers call dispatch(); consumers subscribe().
// All SessionEvent variants are delivered to every subscriber on a
// dedicated worker thread — NOT on the caller's thread.
//
// Raw PCM (AudioFrame payload) never passes through here; it stays on
// the SPSC RingBuffer hot path owned by MediaSession / AudioWorkerPool.
class IDispatcher : public IComponent {
public:
    using Subscriber   = std::function<void(const SessionEvent&)>;
    using SubscriberId = uint32_t;

    virtual ~IDispatcher() = default;

    // Post an event to the queue.  Thread-safe; never blocks the caller.
    virtual void dispatch(SessionEvent event) = 0;

    // Register a handler delivered on the Dispatcher's worker thread.
    // Returns an ID that can be passed to unsubscribe().
    virtual SubscriberId subscribe(Subscriber handler)     = 0;
    virtual void         unsubscribe(SubscriberId id)      = 0;
};

} // namespace voiceai
