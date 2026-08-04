find_path(ONNXRUNTIME_INCLUDE_DIR onnxruntime_cxx_api.h
    PATHS /opt/homebrew/include/onnxruntime /usr/local/include/onnxruntime
    REQUIRED)
find_library(ONNXRUNTIME_LIB onnxruntime
    PATHS /opt/homebrew/lib /usr/local/lib
    REQUIRED)

# Phase 5: RedisClient (config-plane cache reads).
pkg_check_modules(HIREDIS REQUIRED hiredis)

# Explicit source list — never use GLOB so CMake reruns when files are added/removed.
set(GATEWAY_SOURCES
    src/application/Application.cpp
    src/common/ThreadUtils.cpp
    src/config/Config.cpp
    src/config/RedisClient.cpp
    src/core/Application.cpp
    src/dispatcher/Dispatcher.cpp
    src/logging/Logger.cpp
    src/media/AudioWorkerPool.cpp
    src/media/EnergyVAD.cpp
    src/media/MediaSession.cpp
    src/media/SileroVAD.cpp
    src/media/PlaybackDrain.cpp
    src/media/PlaybackQueue.cpp
    src/metrics/Metrics.cpp
    src/session/CallFSM.cpp
    src/session/CallSession.cpp
    src/session/CallSessionFactory.cpp
    src/session/SessionManager.cpp
    src/telephony/ColdTransferCoordinator.cpp
    src/telephony/EslClient.cpp
    src/telephony/EslEventListener.cpp
    src/telephony/TransferCorrelator.cpp
    src/telephony/WarmTransferCoordinator.cpp
    src/timer/TimerService.cpp
    src/transport/NullConversationTransport.cpp
    src/utils/ThreadPool.cpp
    src/websocket/WebSocketConnection.cpp
    src/websocket/WebSocketServer.cpp
)

add_library(gateway_lib STATIC ${GATEWAY_SOURCES})

target_include_directories(gateway_lib
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/include
    PRIVATE
        ${LIBWEBSOCKETS_INCLUDE_DIRS}
)
# SYSTEM: onnxruntime and hiredis headers do not survive -Werror=sign-conversion
# (hiredis's sds.h uses C99 flexible array members + implicit signedness
# conversions throughout — third-party C, not something to patch here).
target_include_directories(gateway_lib SYSTEM PRIVATE ${ONNXRUNTIME_INCLUDE_DIR} ${HIREDIS_INCLUDE_DIRS})

target_link_libraries(gateway_lib
    PUBLIC
        spdlog::spdlog
        yaml-cpp::yaml-cpp
        nlohmann_json::nlohmann_json
    PRIVATE
        ${LIBWEBSOCKETS_LINK_LIBRARIES}
        ${HIREDIS_LINK_LIBRARIES}
        ${ONNXRUNTIME_LIB}
        pthread
)

# ---------------------------------------------------------------------------
# grpc_transport — separate lib so gRPC only links into the main executable,
# not into gateway_tests (gRPC global init hangs under ASan).
# ---------------------------------------------------------------------------
add_library(grpc_transport STATIC
    src/transport/GrpcConversationTransport.cpp
)
target_include_directories(grpc_transport
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/include
        ${CMAKE_BINARY_DIR}/generated
)
target_link_libraries(grpc_transport
    PUBLIC
        gateway_lib
        voiceai_proto
)
target_compile_features(grpc_transport PUBLIC cxx_std_20)
# Suppress warnings we can't control in generated gRPC/protobuf headers.
target_compile_options(grpc_transport PRIVATE -Wno-error)

target_link_directories(gateway_lib PRIVATE ${LIBWEBSOCKETS_LIBRARY_DIRS} ${HIREDIS_LIBRARY_DIRS})

target_compile_features(gateway_lib PUBLIC cxx_std_20)

add_executable(voice_ai_gateway main.cpp)

# gateway_lib arrives transitively via grpc_transport's PUBLIC link —
# listing it here too made the linker warn about duplicate libraries.
target_link_libraries(voice_ai_gateway
    PRIVATE
        grpc_transport
)
