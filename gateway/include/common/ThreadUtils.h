#pragma once

#include <string>

namespace voiceai {

// Sets the OS-visible name of the calling thread (the thread that calls this function).
// Makes LLDB / Instruments / top output readable during debugging.
// Silently no-ops on unsupported platforms.
// On Linux the name is silently truncated to 15 bytes + null terminator.
void set_thread_name(const std::string& name);

} // namespace voiceai
