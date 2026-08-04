#include "common/ThreadUtils.h"
#include "common/platform.h"

#if defined(VOICEAI_PLATFORM_MACOS) || defined(VOICEAI_PLATFORM_LINUX)
#  include <pthread.h>
#endif

namespace voiceai {

void set_thread_name(const std::string& name) {
#if defined(VOICEAI_PLATFORM_MACOS)
    // macOS: sets the name of the calling thread.
    pthread_setname_np(name.c_str());
#elif defined(VOICEAI_PLATFORM_LINUX)
    // Linux: name must be at most 15 chars + null terminator.
    pthread_setname_np(pthread_self(), name.substr(0, 15).c_str());
#else
    (void)name;
#endif
}

} // namespace voiceai
