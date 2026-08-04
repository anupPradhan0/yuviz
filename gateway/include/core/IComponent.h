#pragma once

namespace voiceai {

// Lifecycle contract every long-running subsystem must implement.
// Startup order (enforced by Application): initialize → start.
// Shutdown order is the reverse of startup.
class IComponent {
public:
    virtual ~IComponent() = default;

    // One-time setup; called before start(). Returns false on failure.
    virtual bool initialize() = 0;

    // Begin operation (spawn threads, bind ports, etc.). Returns false on failure.
    virtual bool start() = 0;

    // Graceful stop; must be idempotent.
    virtual void stop() = 0;

    // Final resource release; called after stop().
    virtual void shutdown() = 0;
};

} // namespace voiceai
