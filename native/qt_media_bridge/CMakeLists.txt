cmake_minimum_required(VERSION 3.25)

project(solin_qt_media_bridge VERSION 0.1.0 LANGUAGES CXX)

if(NOT WIN32 OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "The Qt media bridge currently requires Windows x64")
endif()

set(SOLIN_QT_BRIDGE_SDK_ROOT "" CACHE PATH
    "Root of the pinned Qt 6.11.1 MSVC 2022 x64 SDK")
set(SOLIN_SHIBOKEN_PACKAGE_ROOT "" CACHE PATH
    "Python site-packages directory containing the matching Shiboken6 SDK")

if(NOT SOLIN_QT_BRIDGE_SDK_ROOT)
    message(FATAL_ERROR "SOLIN_QT_BRIDGE_SDK_ROOT is required")
endif()
if(NOT SOLIN_SHIBOKEN_PACKAGE_ROOT)
    message(FATAL_ERROR "SOLIN_SHIBOKEN_PACKAGE_ROOT is required")
endif()

find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter Development.Module)
find_package(
    Qt6 6.11.1 EXACT REQUIRED
    COMPONENTS Core Gui Multimedia CorePrivate GuiPrivate MultimediaPrivate
    PATHS "${SOLIN_QT_BRIDGE_SDK_ROOT}"
    NO_DEFAULT_PATH
)
find_package(
    Shiboken6 6.11.1 EXACT REQUIRED
    PATHS "${SOLIN_SHIBOKEN_PACKAGE_ROOT}/shiboken6/lib/cmake"
    NO_DEFAULT_PATH
)

Python3_add_library(solin_qt_media_bridge MODULE WITH_SOABI
    src/bridge.hpp
    src/python_module.cpp
    src/windows/d3d11_bridge.cpp
    ../media_transport/include/solin/media_transport/d3d11_frame_channel_protocol.hpp
)

target_compile_features(solin_qt_media_bridge PRIVATE cxx_std_23)
target_compile_definitions(solin_qt_media_bridge PRIVATE
    NOMINMAX
    WIN32_LEAN_AND_MEAN
)
target_include_directories(solin_qt_media_bridge PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../media_transport/include"
)
target_link_libraries(solin_qt_media_bridge PRIVATE
    Python3::Module
    Shiboken6::libshiboken
    Qt6::Core
    Qt6::GuiPrivate
    Qt6::MultimediaPrivate
    d3d11
    dxgi
    ole32
)

if(MSVC)
    target_compile_options(solin_qt_media_bridge PRIVATE
        /W4
        /WX
        /permissive-
        /EHsc
        /Zc:__cplusplus
    )
else()
    target_compile_options(solin_qt_media_bridge PRIVATE
        -Wall
        -Wextra
        -Wpedantic
        -Werror
    )
endif()

set_target_properties(solin_qt_media_bridge PROPERTIES
    OUTPUT_NAME "solin_qt_media_bridge"
)
