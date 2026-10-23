cmake_minimum_required(VERSION 3.25)

project(solin_virtual_camera VERSION 0.1.0 LANGUAGES CXX)

option(SOLIN_VIRTUAL_CAMERA_BUILD_TESTS "Build virtual-camera transport and filter tests" ON)
option(SOLIN_VIRTUAL_CAMERA_QUALIFY_PRODUCTION
    "Qualify the production filter using the current user's broker endpoint" OFF
)

function(solin_camera_target target)
    target_compile_features(${target} PUBLIC cxx_std_20)
    if(MSVC)
        target_compile_options(${target} PRIVATE /W4 /WX /permissive- /EHsc)
        set_property(TARGET ${target} PROPERTY
            MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
        )
    else()
        target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic -Werror)
    endif()
endfunction()

add_library(solin_virtual_camera_transport STATIC
    src/shared_video_frame_channel.cpp
    src/video_frame.cpp
    src/virtual_camera_broker_protocol.cpp
    src/windows_virtual_camera_contract.cpp
)
target_include_directories(solin_virtual_camera_transport
    PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
solin_camera_target(solin_virtual_camera_transport)
if(WIN32)
    target_compile_definitions(solin_virtual_camera_transport
        PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX
                WINVER=0x0A00 _WIN32_WINNT=0x0A00
    )
    target_link_libraries(solin_virtual_camera_transport PRIVATE advapi32 bcrypt ole32)
endif()

if(SOLIN_VIRTUAL_CAMERA_BUILD_TESTS)
    enable_testing()
    foreach(component video_frame shared_video_frame_channel virtual_camera_broker_protocol)
        add_executable(solin-${component}-tests tests/${component}_tests.cpp)
        target_link_libraries(solin-${component}-tests PRIVATE solin_virtual_camera_transport)
        solin_camera_target(solin-${component}-tests)
        add_test(NAME ${component} COMMAND solin-${component}-tests)
    endforeach()
endif()

if(WIN32)
    include(cmake/SolinVirtualCameraFilter.cmake)
    solin_add_virtual_camera_filter()
endif()

find_program(SOLIN_CLANG_FORMAT_EXECUTABLE NAMES clang-format)
if(SOLIN_CLANG_FORMAT_EXECUTABLE)
    file(GLOB_RECURSE camera_format_sources CONFIGURE_DEPENDS
        include/*.hpp src/*.cpp tests/*.cpp windows/*.hpp windows/*.cpp
    )
    add_custom_target(solin-virtual-camera-format
        COMMAND ${SOLIN_CLANG_FORMAT_EXECUTABLE} -i ${camera_format_sources}
        COMMENT "Formatting virtual-camera sources"
        VERBATIM
    )
endif()
