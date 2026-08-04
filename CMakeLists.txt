cmake_minimum_required(VERSION 3.20)
project(VoiceAIPlatform VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# ---------------------------------------------------------------------------
# Warnings — applied to all targets in all build types.
# ---------------------------------------------------------------------------
add_compile_options(
    -Wall
    -Wextra
    -Wpedantic
    -Wconversion
    -Wshadow
    -Wno-unused-parameter
)

# ---------------------------------------------------------------------------
# Debug-only: promote all warnings to errors + memory/UB sanitizers.
# ---------------------------------------------------------------------------
if(CMAKE_BUILD_TYPE STREQUAL "Debug")
    add_compile_options(
        -Werror
        -fno-omit-frame-pointer
        -fsanitize=address,undefined
    )
    add_link_options(-fsanitize=address,undefined)
endif()

find_package(spdlog REQUIRED)
find_package(yaml-cpp REQUIRED)
find_package(nlohmann_json REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(LIBWEBSOCKETS REQUIRED libwebsockets)

# ---------------------------------------------------------------------------
# gRPC + Protobuf (Phase 3)
# gRPC's cmake config loads protobuf transitively — don't call find_package(Protobuf)
# separately or you get duplicate-target errors between the two protobuf installs.
# ---------------------------------------------------------------------------
find_package(gRPC CONFIG REQUIRED)

# Resolve toolchain executables. get_target_property IMPORTED_LOCATION is
# unreliable for multi-config installs; use find_program instead.
find_program(Protobuf_PROTOC_EXECUTABLE  NAMES protoc       REQUIRED)
find_program(GRPC_CPP_PLUGIN_EXECUTABLE  NAMES grpc_cpp_plugin REQUIRED)

# Helper: generate .pb.cc / .grpc.pb.cc from a .proto file.
# Usage:
#   add_proto_library(target proto/voiceai/v1/conversation.proto)
# Produces a static library 'target' that downstream targets can link against.
function(add_proto_library TARGET PROTO_FILE)
    get_filename_component(PROTO_ABS "${PROTO_FILE}" ABSOLUTE)
    get_filename_component(PROTO_DIR "${PROTO_ABS}" DIRECTORY)
    get_filename_component(PROTO_NAME_WE "${PROTO_ABS}" NAME_WE)

    set(PROTO_INCLUDE_DIR "${CMAKE_SOURCE_DIR}/proto")
    set(GENERATED_DIR     "${CMAKE_BINARY_DIR}/generated")
    file(MAKE_DIRECTORY "${GENERATED_DIR}")

    # Reconstruct the subdirectory under proto/ so generated files mirror it.
    file(RELATIVE_PATH PROTO_REL "${PROTO_INCLUDE_DIR}" "${PROTO_ABS}")
    get_filename_component(PROTO_REL_DIR "${PROTO_REL}" DIRECTORY)

    set(PB_CC  "${GENERATED_DIR}/${PROTO_REL_DIR}/${PROTO_NAME_WE}.pb.cc")
    set(PB_H   "${GENERATED_DIR}/${PROTO_REL_DIR}/${PROTO_NAME_WE}.pb.h")
    set(GPB_CC "${GENERATED_DIR}/${PROTO_REL_DIR}/${PROTO_NAME_WE}.grpc.pb.cc")
    set(GPB_H  "${GENERATED_DIR}/${PROTO_REL_DIR}/${PROTO_NAME_WE}.grpc.pb.h")

    add_custom_command(
        OUTPUT  "${PB_CC}" "${PB_H}" "${GPB_CC}" "${GPB_H}"
        COMMAND "${Protobuf_PROTOC_EXECUTABLE}"
                "--proto_path=${PROTO_INCLUDE_DIR}"
                "--cpp_out=${GENERATED_DIR}"
                "--grpc_out=${GENERATED_DIR}"
                "--plugin=protoc-gen-grpc=${GRPC_CPP_PLUGIN_EXECUTABLE}"
                "${PROTO_ABS}"
        DEPENDS "${PROTO_ABS}"
        COMMENT "Generating gRPC C++ stubs for ${PROTO_FILE}"
        VERBATIM
    )

    add_library("${TARGET}" STATIC "${PB_CC}" "${GPB_CC}")
    target_include_directories("${TARGET}" PUBLIC "${GENERATED_DIR}")
    target_link_libraries("${TARGET}"
        PUBLIC
            gRPC::grpc++
            protobuf::libprotobuf
    )
    # Suppress warnings we can't fix in generated code.
    target_compile_options("${TARGET}" PRIVATE -w)
endfunction()

# Build the conversation proto stub library.
add_proto_library(voiceai_proto proto/voiceai/v1/conversation.proto)

enable_testing()
find_package(GTest REQUIRED)

add_subdirectory(gateway)
add_subdirectory(tests)
