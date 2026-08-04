#pragma once

// Platform detection guards — include this instead of scattering #ifdef chains.

#if defined(_WIN32) || defined(_WIN64)
#  define VOICEAI_PLATFORM_WINDOWS 1
#elif defined(__APPLE__)
#  define VOICEAI_PLATFORM_MACOS 1
#elif defined(__linux__)
#  define VOICEAI_PLATFORM_LINUX 1
#endif

#if defined(__clang__)
#  define VOICEAI_COMPILER_CLANG 1
#elif defined(__GNUC__)
#  define VOICEAI_COMPILER_GCC 1
#elif defined(_MSC_VER)
#  define VOICEAI_COMPILER_MSVC 1
#endif

#if defined(VOICEAI_PLATFORM_LINUX) || defined(VOICEAI_PLATFORM_MACOS)
#  define VOICEAI_PLATFORM_POSIX 1
#endif
