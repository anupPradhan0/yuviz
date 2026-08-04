#pragma once

#include "core/IComponent.h"
#include <string>

namespace voiceai {

class IMetrics : public IComponent {
public:
    virtual ~IMetrics() = default;

    // IComponent provides: bool initialize(), bool start(), void stop(), void shutdown()

    virtual void increment(const std::string& name, double value = 1.0) = 0;
    virtual void gauge(const std::string& name, double value)           = 0;
    virtual void observe(const std::string& name, double value)         = 0;
};

} // namespace voiceai
