#pragma once

#include "metrics/IMetrics.h"
#include "logging/Logger.h"
#include <mutex>
#include <unordered_map>

namespace voiceai {

class Metrics final : public IMetrics {
public:
    explicit Metrics(Logger& logger);
    ~Metrics() override = default;

    Metrics(const Metrics&)            = delete;
    Metrics& operator=(const Metrics&) = delete;

    bool initialize() override;
    bool start()      override;
    void stop()       override;
    void shutdown()   override;

    void increment(const std::string& name, double value = 1.0) override;
    void gauge(const std::string& name, double value)           override;
    void observe(const std::string& name, double value)         override;

    // Returns a snapshot of all counter and gauge values for inspection.
    [[nodiscard]] std::unordered_map<std::string, double> snapshot() const;

private:
    Logger&                                  logger_;
    mutable std::mutex                       mutex_;
    std::unordered_map<std::string, double>  counters_;
    std::unordered_map<std::string, double>  gauges_;
    std::unordered_map<std::string, double>  histograms_;
};

} // namespace voiceai
