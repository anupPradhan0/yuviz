#pragma once

#include <cstdint>

namespace voiceai {

inline constexpr uint32_t kDefaultSampleRate    = 8000;
inline constexpr uint8_t  kDefaultChannels      = 1;
inline constexpr uint32_t kDefaultFrameMs       = 20;
inline constexpr uint32_t kDefaultRingBufferMs  = 500;
inline constexpr uint16_t kDefaultWebSocketPort = 8080;
inline constexpr uint32_t kDefaultMaxConns      = 1000;
inline constexpr uint32_t kDefaultTimeoutMs     = 30000;

} // namespace voiceai
