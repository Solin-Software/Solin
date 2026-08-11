cmake_minimum_required(VERSION 3.25)

project(solin_media_engine VERSION 0.1.0 LANGUAGES CXX)

option(SOLIN_MEDIA_ENGINE_BUILD_TESTS "Build native media-engine tests" ON)
option(SOLIN_MEDIA_ENGINE_ENABLE_GSTREAMER "Build the native GStreamer media substrate" OFF)
set(SOLIN_GSTREAMER_ROOT "" CACHE PATH "Pinned GStreamer MSVC installation root")

set(SOLIN_MEDIA_ENGINE_SOURCES
    include/solin/media_engine/control_protocol.hpp
    include/solin/media_engine/frame_codec.hpp
    include/solin/media_engine/frame_channel.hpp
    include/solin/media_engine/frame_channel_output.hpp
    include/solin/media_engine/media_runtime.hpp
    include/solin/media_engine/native_window_output.hpp
    include/solin/media_engine/render_geometry.hpp
    include/solin/media_engine/scene_graph.hpp
    include/solin/media_engine/scene_renderer.hpp
    include/solin/media_engine/scene_snapshot.hpp
    include/solin/media_engine/shared_video_frame_channel.hpp
    include/solin/media_engine/source_registry.hpp
    include/solin/media_engine/video_frame.hpp
    include/solin/media_engine/virtual_camera.hpp
    include/solin/media_engine/virtual_camera_output.hpp
    include/solin/media_engine/virtual_camera_broker_protocol.hpp
    include/solin/media_engine/virtual_camera_broker.hpp
    src/control_protocol.cpp
    src/frame_codec.cpp
    src/frame_channel.cpp
    src/gstreamer_source_runtime.cpp
    src/gstreamer_source_runtime.hpp
    src/gstreamer_scene_renderer.cpp
    src/gstreamer_scene_renderer.hpp
    src/main.cpp
    src/frame_channel_output.cpp
    src/media_runtime.cpp
    src/native_window_output.cpp
    src/render_geometry.cpp
    src/scene_graph.cpp
    src/scene_renderer.cpp
    src/scene_snapshot.cpp
    src/shared_video_frame_channel.cpp
    src/source_registry.cpp
    src/video_frame.cpp
    src/virtual_camera.cpp
    src/virtual_camera_output.cpp
    src/virtual_camera_broker_protocol.cpp
    src/virtual_camera_broker.cpp
    src/windows_virtual_camera_backend.cpp
    src/windows_virtual_camera_backend.hpp
    tests/frame_codec_tests.cpp
    tests/frame_channel_tests.cpp
    tests/gstreamer_source_runtime_tests.cpp
    tests/render_geometry_tests.cpp
    tests/scene_graph_tests.cpp
    tests/scene_snapshot_tests.cpp
    tests/shared_video_frame_channel_tests.cpp
    tests/source_registry_tests.cpp
    tests/video_frame_tests.cpp
    tests/virtual_camera_tests.cpp
    tests/virtual_camera_broker_protocol_tests.cpp
    tests/virtual_camera_broker_tests.cpp
    windows/virtual_camera_source/activation.cpp
    windows/virtual_camera_source/activation.hpp
    windows/virtual_camera_source/broker_client.cpp
    windows/virtual_camera_source/broker_client.hpp
    windows/virtual_camera_source/dllmain.cpp
    windows/virtual_camera_source/media_source.cpp
    windows/virtual_camera_source/media_source.hpp
    windows/virtual_camera_source/media_stream.cpp
    windows/virtual_camera_source/media_stream.hpp
    include/solin/media_engine/windows_virtual_camera_contract.hpp
)

find_program(SOLIN_CLANG_FORMAT_EXECUTABLE NAMES clang-format)
if(SOLIN_CLANG_FORMAT_EXECUTABLE)
    add_custom_target(solin-media-engine-format
        COMMAND ${SOLIN_CLANG_FORMAT_EXECUTABLE} -i ${SOLIN_MEDIA_ENGINE_SOURCES}
        WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
        COMMENT "Formatting native media-engine sources"
        VERBATIM
    )
endif()

include(FetchContent)
set(JSON_BuildTests OFF CACHE INTERNAL "Disable dependency tests")
FetchContent_Declare(
    nlohmann_json
    URL https://github.com/nlohmann/json/releases/download/v3.12.0/json.tar.xz
    URL_HASH SHA256=42f6e95cad6ec532fd372391373363b62a14af6d771056dbfc86160e6dfff7aa
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
)
FetchContent_MakeAvailable(nlohmann_json)

if(SOLIN_MEDIA_ENGINE_ENABLE_GSTREAMER)
    include(cmake/FindSolinGStreamer.cmake)
endif()

add_library(solin_media_engine_protocol STATIC
    src/control_protocol.cpp
    src/frame_codec.cpp
)
target_include_directories(solin_media_engine_protocol
    PUBLIC
        "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>"
        "$<INSTALL_INTERFACE:include>"
)
target_compile_features(solin_media_engine_protocol PUBLIC cxx_std_20)
target_link_libraries(solin_media_engine_protocol PUBLIC nlohmann_json::nlohmann_json)

add_library(solin_media_engine_scene STATIC
    src/scene_snapshot.cpp
)
target_include_directories(solin_media_engine_scene
    PUBLIC
        "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>"
        "$<INSTALL_INTERFACE:include>"
)
target_compile_features(solin_media_engine_scene PUBLIC cxx_std_20)
target_link_libraries(solin_media_engine_scene PUBLIC nlohmann_json::nlohmann_json)

add_library(solin_media_engine_graph STATIC
    src/render_geometry.cpp
    src/scene_graph.cpp
    src/scene_renderer.cpp
    src/source_registry.cpp
)
target_include_directories(solin_media_engine_graph
    PUBLIC
        "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>"
        "$<INSTALL_INTERFACE:include>"
)
target_compile_features(solin_media_engine_graph PUBLIC cxx_std_20)
target_link_libraries(solin_media_engine_graph PUBLIC solin_media_engine_scene)

add_library(solin_media_engine_transport STATIC
    src/frame_channel.cpp
    src/shared_video_frame_channel.cpp
    src/video_frame.cpp
    src/virtual_camera_broker_protocol.cpp
)
target_include_directories(solin_media_engine_transport
    PUBLIC
        "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>"
        "$<INSTALL_INTERFACE:include>"
)
target_compile_features(solin_media_engine_transport PUBLIC cxx_std_20)
target_link_libraries(solin_media_engine_transport PUBLIC solin_media_engine_scene)

add_library(solin_media_engine_runtime STATIC
    src/gstreamer_scene_renderer.cpp
    src/gstreamer_source_runtime.cpp
    src/media_runtime.cpp
    src/native_window_output.cpp
)
target_include_directories(solin_media_engine_runtime
    PUBLIC
        "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>"
        "$<INSTALL_INTERFACE:include>"
)
target_compile_features(solin_media_engine_runtime PUBLIC cxx_std_20)
target_link_libraries(
    solin_media_engine_runtime
    PUBLIC solin_media_engine_graph solin_media_engine_transport solin_media_engine_outputs
)

add_library(solin_media_engine_outputs STATIC
    src/frame_channel_output.cpp
    src/virtual_camera.cpp
    src/virtual_camera_broker.cpp
    src/virtual_camera_output.cpp
)
target_include_directories(solin_media_engine_outputs
    PUBLIC
        "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>"
        "$<INSTALL_INTERFACE:include>"
)
target_compile_features(solin_media_engine_outputs PUBLIC cxx_std_20)
target_link_libraries(
    solin_media_engine_outputs
    PUBLIC solin_media_engine_scene solin_media_engine_transport
)
if(WIN32)
    target_sources(solin_media_engine_outputs PRIVATE src/windows_virtual_camera_backend.cpp)
    target_link_libraries(
        solin_media_engine_outputs PRIVATE advapi32 bcrypt mfplat mfuuid ole32
    )

    add_library(solin_virtual_camera_source_transport STATIC
        src/shared_video_frame_channel.cpp
        src/video_frame.cpp
        src/virtual_camera_broker_protocol.cpp
    )
    target_include_directories(solin_virtual_camera_source_transport
        PUBLIC "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>"
    )
    target_compile_features(solin_virtual_camera_source_transport PUBLIC cxx_std_20)
    target_link_libraries(
        solin_virtual_camera_source_transport PRIVATE advapi32 ole32
    )
    set_property(TARGET solin_virtual_camera_source_transport PROPERTY
        MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
    )
    target_compile_options(
        solin_virtual_camera_source_transport PRIVATE /W4 /WX /permissive- /EHsc
    )

    add_library(solin-virtual-camera-source SHARED
        windows/virtual_camera_source/activation.cpp
        windows/virtual_camera_source/broker_client.cpp
        windows/virtual_camera_source/dllmain.cpp
        windows/virtual_camera_source/media_source.cpp
        windows/virtual_camera_source/media_stream.cpp
        windows/virtual_camera_source/standby_resources.rc
        windows/virtual_camera_source/virtual_camera_source.def
    )
    target_include_directories(solin-virtual-camera-source
        PRIVATE
            ${CMAKE_CURRENT_SOURCE_DIR}/include
            ${CMAKE_CURRENT_SOURCE_DIR}/windows/virtual_camera_source
    )
    target_compile_features(solin-virtual-camera-source PRIVATE cxx_std_20)
    target_compile_definitions(
        solin-virtual-camera-source PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX
    )
    set_property(TARGET solin-virtual-camera-source PROPERTY
        MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
    )
    target_compile_options(
        solin-virtual-camera-source PRIVATE /W4 /WX /wd4324 /permissive- /EHsc
    )
    target_link_options(
        solin-virtual-camera-source PRIVATE /DYNAMICBASE /NXCOMPAT /CETCOMPAT
    )
    target_link_libraries(
        solin-virtual-camera-source
        PRIVATE
            solin_virtual_camera_source_transport
            bcrypt
            evr
            mfplat
            mfsensorgroup
            mfuuid
            ole32
            runtimeobject
            user32
            gdi32
    )
    set_target_properties(
        solin-virtual-camera-source PROPERTIES PREFIX "" OUTPUT_NAME "solin-virtual-camera-source"
    )
endif()

add_executable(solin-media-engine
    src/main.cpp
)
target_link_libraries(
    solin-media-engine
    PRIVATE
        solin_media_engine_protocol
        solin_media_engine_scene
        solin_media_engine_graph
        solin_media_engine_runtime
        solin_media_engine_outputs
)
target_compile_features(solin-media-engine PRIVATE cxx_std_20)
target_compile_definitions(solin-media-engine PRIVATE
    SOLIN_MEDIA_ENGINE_VERSION="${PROJECT_VERSION}"
)
if(SOLIN_MEDIA_ENGINE_ENABLE_GSTREAMER)
    target_compile_definitions(
        solin_media_engine_runtime
        PRIVATE
            SOLIN_MEDIA_ENGINE_HAS_GSTREAMER=1
            GST_USE_UNSTABLE_API=1
    )
    target_link_libraries(solin_media_engine_runtime PRIVATE Solin::GStreamer)
endif()

if(MSVC)
    target_compile_options(solin_media_engine_protocol PRIVATE /W4 /WX /permissive- /EHsc)
    target_compile_options(solin_media_engine_scene PRIVATE /W4 /WX /permissive- /EHsc)
    target_compile_options(solin_media_engine_graph PRIVATE /W4 /WX /permissive- /EHsc)
    target_compile_options(solin_media_engine_transport PRIVATE /W4 /WX /permissive- /EHsc)
    target_compile_options(solin_media_engine_runtime PRIVATE /W4 /WX /permissive- /EHsc)
    target_compile_options(solin_media_engine_outputs PRIVATE /W4 /WX /permissive- /EHsc)
    target_compile_options(solin-media-engine PRIVATE /W4 /WX /permissive- /EHsc)
else()
    target_compile_options(solin_media_engine_protocol PRIVATE
        -Wall -Wextra -Wpedantic -Werror
    )
    target_compile_options(solin-media-engine PRIVATE
        -Wall -Wextra -Wpedantic -Werror
    )
    target_compile_options(solin_media_engine_scene PRIVATE
        -Wall -Wextra -Wpedantic -Werror
    )
    target_compile_options(solin_media_engine_graph PRIVATE
        -Wall -Wextra -Wpedantic -Werror
    )
    target_compile_options(solin_media_engine_transport PRIVATE
        -Wall -Wextra -Wpedantic -Werror
    )
    target_compile_options(solin_media_engine_runtime PRIVATE
        -Wall -Wextra -Wpedantic -Werror
    )
    target_compile_options(solin_media_engine_outputs PRIVATE
        -Wall -Wextra -Wpedantic -Werror
    )
endif()

install(TARGETS solin-media-engine RUNTIME DESTINATION bin)
if(WIN32)
    install(TARGETS solin-virtual-camera-source RUNTIME DESTINATION virtual-camera)
endif()
install(
    FILES third_party/nlohmann-json.LICENSE.MIT
    DESTINATION share/licenses/solin-media-engine
)

if(SOLIN_MEDIA_ENGINE_BUILD_TESTS)
    enable_testing()
    add_executable(solin-media-engine-protocol-tests
        tests/frame_codec_tests.cpp
    )
    target_link_libraries(
        solin-media-engine-protocol-tests
        PRIVATE solin_media_engine_protocol
    )
    target_compile_features(solin-media-engine-protocol-tests PRIVATE cxx_std_20)
    if(MSVC)
        target_compile_options(
            solin-media-engine-protocol-tests PRIVATE /W4 /WX /permissive- /EHsc
        )
    else()
        target_compile_options(
            solin-media-engine-protocol-tests PRIVATE
            -Wall -Wextra -Wpedantic -Werror
        )
    endif()
    add_test(NAME frame-codec COMMAND solin-media-engine-protocol-tests)
    add_executable(solin-media-engine-frame-channel-tests
        tests/frame_channel_tests.cpp
    )
    target_link_libraries(
        solin-media-engine-frame-channel-tests
        PRIVATE solin_media_engine_transport
    )
    target_compile_features(
        solin-media-engine-frame-channel-tests PRIVATE cxx_std_20
    )
    if(MSVC)
        target_compile_options(
            solin-media-engine-frame-channel-tests PRIVATE /W4 /WX /permissive- /EHsc
        )
    else()
        target_compile_options(
            solin-media-engine-frame-channel-tests PRIVATE
            -Wall -Wextra -Wpedantic -Werror
        )
    endif()
    add_test(NAME frame-channel COMMAND solin-media-engine-frame-channel-tests)
    add_executable(solin-media-engine-video-frame-tests
        tests/video_frame_tests.cpp
    )
    target_link_libraries(
        solin-media-engine-video-frame-tests
        PRIVATE solin_media_engine_transport
    )
    target_compile_features(solin-media-engine-video-frame-tests PRIVATE cxx_std_20)
    if(MSVC)
        target_compile_options(
            solin-media-engine-video-frame-tests PRIVATE /W4 /WX /permissive- /EHsc
        )
    else()
        target_compile_options(
            solin-media-engine-video-frame-tests PRIVATE
            -Wall -Wextra -Wpedantic -Werror
        )
    endif()
    add_test(NAME video-frame COMMAND solin-media-engine-video-frame-tests)
    add_executable(solin-media-engine-virtual-camera-tests
        tests/virtual_camera_tests.cpp
    )
    target_link_libraries(
        solin-media-engine-virtual-camera-tests
        PRIVATE solin_media_engine_outputs
    )
    if(WIN32)
        target_include_directories(
            solin-media-engine-virtual-camera-tests
            PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src
        )
        target_link_libraries(
            solin-media-engine-virtual-camera-tests PRIVATE mfplat
        )
    endif()
    target_compile_features(
        solin-media-engine-virtual-camera-tests PRIVATE cxx_std_20
    )
    if(MSVC)
        target_compile_options(
            solin-media-engine-virtual-camera-tests
            PRIVATE /W4 /WX /permissive- /EHsc
        )
    else()
        target_compile_options(
            solin-media-engine-virtual-camera-tests PRIVATE
            -Wall -Wextra -Wpedantic -Werror
        )
    endif()
    add_test(NAME virtual-camera COMMAND solin-media-engine-virtual-camera-tests)
    add_executable(solin-media-engine-virtual-camera-output-tests
        tests/virtual_camera_output_tests.cpp
    )
    target_link_libraries(
        solin-media-engine-virtual-camera-output-tests
        PRIVATE solin_media_engine_outputs
    )
    target_compile_features(
        solin-media-engine-virtual-camera-output-tests PRIVATE cxx_std_20
    )
    if(MSVC)
        target_compile_options(
            solin-media-engine-virtual-camera-output-tests
            PRIVATE /W4 /WX /permissive- /EHsc
        )
    else()
        target_compile_options(
            solin-media-engine-virtual-camera-output-tests PRIVATE
            -Wall -Wextra -Wpedantic -Werror
        )
    endif()
    add_test(
        NAME virtual-camera-output
        COMMAND solin-media-engine-virtual-camera-output-tests
    )
    add_executable(solin-media-engine-virtual-camera-broker-protocol-tests
        tests/virtual_camera_broker_protocol_tests.cpp
    )
    target_link_libraries(
        solin-media-engine-virtual-camera-broker-protocol-tests
        PRIVATE solin_media_engine_transport
    )
    target_compile_features(
        solin-media-engine-virtual-camera-broker-protocol-tests PRIVATE cxx_std_20
    )
    if(MSVC)
        target_compile_options(
            solin-media-engine-virtual-camera-broker-protocol-tests
            PRIVATE /W4 /WX /permissive- /EHsc
        )
    else()
        target_compile_options(
            solin-media-engine-virtual-camera-broker-protocol-tests PRIVATE
            -Wall -Wextra -Wpedantic -Werror
        )
    endif()
    add_test(
        NAME virtual-camera-broker-protocol
        COMMAND solin-media-engine-virtual-camera-broker-protocol-tests
    )
    add_executable(solin-media-engine-virtual-camera-broker-tests
        tests/virtual_camera_broker_tests.cpp
    )
    target_link_libraries(
        solin-media-engine-virtual-camera-broker-tests
        PRIVATE solin_media_engine_outputs
    )
    target_compile_features(
        solin-media-engine-virtual-camera-broker-tests PRIVATE cxx_std_20
    )
    if(MSVC)
        target_compile_options(
            solin-media-engine-virtual-camera-broker-tests
            PRIVATE /W4 /WX /permissive- /EHsc
        )
    else()
        target_compile_options(
            solin-media-engine-virtual-camera-broker-tests PRIVATE
            -Wall -Wextra -Wpedantic -Werror
        )
    endif()
    add_test(
        NAME virtual-camera-broker
        COMMAND solin-media-engine-virtual-camera-broker-tests
    )
    if(WIN32)
        add_executable(solin-media-engine-virtual-camera-source-tests
            tests/virtual_camera_source_tests.cpp
        )
        target_include_directories(
            solin-media-engine-virtual-camera-source-tests
            PRIVATE
                ${CMAKE_CURRENT_SOURCE_DIR}/include
                ${CMAKE_CURRENT_SOURCE_DIR}/windows/virtual_camera_source
        )
        target_compile_features(
            solin-media-engine-virtual-camera-source-tests PRIVATE cxx_std_20
        )
        target_compile_options(
            solin-media-engine-virtual-camera-source-tests
            PRIVATE /W4 /WX /permissive- /EHsc
        )
        target_compile_definitions(
            solin-media-engine-virtual-camera-source-tests
            PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX
        )
        target_link_libraries(
            solin-media-engine-virtual-camera-source-tests
            PRIVATE d3d11 mfplat mfuuid ole32
        )
        add_dependencies(
            solin-media-engine-virtual-camera-source-tests
            solin-virtual-camera-source
        )
        add_test(
            NAME virtual-camera-source
            COMMAND solin-media-engine-virtual-camera-source-tests
                    $<TARGET_FILE:solin-virtual-camera-source>
        )
    endif()
    add_executable(solin-media-engine-shared-video-frame-channel-tests
        tests/shared_video_frame_channel_tests.cpp
    )
    target_link_libraries(
        solin-media-engine-shared-video-frame-channel-tests
        PRIVATE solin_media_engine_transport
    )
    target_compile_features(
        solin-media-engine-shared-video-frame-channel-tests PRIVATE cxx_std_20
    )
    if(MSVC)
        target_compile_options(
            solin-media-engine-shared-video-frame-channel-tests
            PRIVATE /W4 /WX /permissive- /EHsc
        )
    else()
        target_compile_options(
            solin-media-engine-shared-video-frame-channel-tests PRIVATE
            -Wall -Wextra -Wpedantic -Werror
        )
    endif()
    add_test(
        NAME shared-video-frame-channel
        COMMAND solin-media-engine-shared-video-frame-channel-tests
    )
    add_executable(solin-media-engine-scene-tests
        tests/scene_snapshot_tests.cpp
    )
    target_link_libraries(
        solin-media-engine-scene-tests
        PRIVATE solin_media_engine_scene
    )
    target_compile_features(solin-media-engine-scene-tests PRIVATE cxx_std_20)
    if(MSVC)
        target_compile_options(
            solin-media-engine-scene-tests PRIVATE /W4 /WX /permissive- /EHsc
        )
    else()
        target_compile_options(
            solin-media-engine-scene-tests PRIVATE
            -Wall -Wextra -Wpedantic -Werror
        )
    endif()
    add_test(NAME scene-snapshot COMMAND solin-media-engine-scene-tests)
    add_executable(solin-media-engine-source-registry-tests
        tests/source_registry_tests.cpp
    )
    target_link_libraries(
        solin-media-engine-source-registry-tests
        PRIVATE solin_media_engine_graph
    )
    target_compile_features(solin-media-engine-source-registry-tests PRIVATE cxx_std_20)
    if(MSVC)
        target_compile_options(
            solin-media-engine-source-registry-tests PRIVATE /W4 /WX /permissive- /EHsc
        )
    else()
        target_compile_options(
            solin-media-engine-source-registry-tests PRIVATE
            -Wall -Wextra -Wpedantic -Werror
        )
    endif()
    add_test(NAME source-registry COMMAND solin-media-engine-source-registry-tests)
    add_executable(solin-media-engine-render-geometry-tests
        tests/render_geometry_tests.cpp
    )
    target_link_libraries(
        solin-media-engine-render-geometry-tests
        PRIVATE solin_media_engine_graph
    )
    target_compile_features(
        solin-media-engine-render-geometry-tests PRIVATE cxx_std_20
    )
    if(MSVC)
        target_compile_options(
            solin-media-engine-render-geometry-tests
            PRIVATE /W4 /WX /permissive- /EHsc
        )
    else()
        target_compile_options(
            solin-media-engine-render-geometry-tests
            PRIVATE -Wall -Wextra -Wpedantic -Werror
        )
    endif()
    add_test(NAME render-geometry COMMAND solin-media-engine-render-geometry-tests)
    add_executable(solin-media-engine-scene-graph-tests
        tests/scene_graph_tests.cpp
    )
    target_link_libraries(
        solin-media-engine-scene-graph-tests
        PRIVATE solin_media_engine_graph
    )
    target_compile_features(solin-media-engine-scene-graph-tests PRIVATE cxx_std_20)
    if(MSVC)
        target_compile_options(
            solin-media-engine-scene-graph-tests PRIVATE /W4 /WX /permissive- /EHsc
        )
    else()
        target_compile_options(
            solin-media-engine-scene-graph-tests PRIVATE
            -Wall -Wextra -Wpedantic -Werror
        )
    endif()
    add_test(NAME scene-graph COMMAND solin-media-engine-scene-graph-tests)
    add_test(NAME executable-self-test COMMAND solin-media-engine --self-test)
    if(SOLIN_MEDIA_ENGINE_ENABLE_GSTREAMER)
        add_executable(solin-media-engine-gstreamer-source-tests
            tests/gstreamer_source_runtime_tests.cpp
        )
        target_link_libraries(
            solin-media-engine-gstreamer-source-tests
            PRIVATE
                solin_media_engine_runtime
                solin_media_engine_outputs
                Solin::GStreamer
                Solin::GStreamerRtspServer
        )
        target_compile_features(
            solin-media-engine-gstreamer-source-tests PRIVATE cxx_std_20
        )
        target_include_directories(
            solin-media-engine-gstreamer-source-tests
            PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src
        )
        if(MSVC)
            target_compile_options(
                solin-media-engine-gstreamer-source-tests
                PRIVATE /W4 /WX /permissive- /EHsc
            )
        else()
            target_compile_options(
                solin-media-engine-gstreamer-source-tests
                PRIVATE -Wall -Wextra -Wpedantic -Werror
            )
        endif()
        add_test(NAME gstreamer-source-runtime
                 COMMAND solin-media-engine-gstreamer-source-tests)
        set_property(TEST gstreamer-source-runtime PROPERTY ENVIRONMENT_MODIFICATION
            "PATH=path_list_prepend:${SOLIN_GSTREAMER_ROOT}/bin"
            "PATH=path_list_prepend:${SOLIN_GSTREAMER_ROOT}/lib/libproxy"
            "GST_PLUGIN_PATH_1_0=set:"
            "GST_PLUGIN_SYSTEM_PATH_1_0=set:${SOLIN_GSTREAMER_ROOT}/lib/gstreamer-1.0"
            "GST_REGISTRY_1_0=set:${CMAKE_CURRENT_BINARY_DIR}/gst-registry.bin"
        )
        set_property(TEST executable-self-test PROPERTY ENVIRONMENT_MODIFICATION
            "PATH=path_list_prepend:${SOLIN_GSTREAMER_ROOT}/bin"
            "PATH=path_list_prepend:${SOLIN_GSTREAMER_ROOT}/lib/libproxy"
            "GST_PLUGIN_PATH_1_0=set:"
            "GST_PLUGIN_SYSTEM_PATH_1_0=set:${SOLIN_GSTREAMER_ROOT}/lib/gstreamer-1.0"
            "GST_REGISTRY_1_0=set:${CMAKE_CURRENT_BINARY_DIR}/gst-registry.bin"
        )
    endif()
endif()
