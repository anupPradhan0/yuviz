#include "telephony/TransferCorrelator.h"

namespace voiceai {

void TransferCorrelator::watch(const std::string& uuid, ResolutionHandler on_resolved) {
    std::lock_guard lock{mutex_};
    pending_[uuid] = std::move(on_resolved);
}

void TransferCorrelator::cancel(const std::string& uuid) {
    std::lock_guard lock{mutex_};
    pending_.erase(uuid);
}

bool TransferCorrelator::resolve(const std::string& uuid, bool success, std::string detail) {
    ResolutionHandler handler;
    {
        std::lock_guard lock{mutex_};
        auto it = pending_.find(uuid);
        if (it == pending_.end()) return false;
        handler = std::move(it->second);
        pending_.erase(it);
    }
    if (handler) handler(success, std::move(detail));
    return true;
}

} // namespace voiceai
