#include "metrics/Metrics.h"

namespace voiceai {

Metrics::Metrics(Logger& logger) : logger_(logger) {}

bool Metrics::initialize() {
    return true;
}

bool Metrics::start() {
    logger_.info("Metrics started");
    return true;
}

void Metrics::stop() {
    logger_.info("Metrics stopped");
}

void Metrics::shutdown() {}

void Metrics::increment(const std::string& name, double value) {
    std::lock_guard lock{mutex_};
    counters_[name] += value;
}

void Metrics::gauge(const std::string& name, double value) {
    std::lock_guard lock{mutex_};
    gauges_[name] = value;
}

void Metrics::observe(const std::string& name, double value) {
    std::lock_guard lock{mutex_};
    histograms_[name] += value;
}

std::unordered_map<std::string, double> Metrics::snapshot() const {
    std::lock_guard lock{mutex_};
    auto snap = counters_;
    snap.insert(gauges_.begin(), gauges_.end());
    return snap;
}

} // namespace voiceai
