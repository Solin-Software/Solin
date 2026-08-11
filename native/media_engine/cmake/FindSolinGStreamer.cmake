if(NOT WIN32)
    message(FATAL_ERROR "The native media-engine GStreamer toolchain currently targets Windows")
endif()

if(NOT SOLIN_GSTREAMER_ROOT AND DEFINED ENV{SOLIN_GSTREAMER_ROOT})
    set(SOLIN_GSTREAMER_ROOT "$ENV{SOLIN_GSTREAMER_ROOT}")
endif()
if(NOT SOLIN_GSTREAMER_ROOT)
    message(FATAL_ERROR
        "Set SOLIN_GSTREAMER_ROOT to the pinned GStreamer MSVC x86_64 installation"
    )
endif()

cmake_path(ABSOLUTE_PATH SOLIN_GSTREAMER_ROOT NORMALIZE)
set(_solin_pkg_config "${SOLIN_GSTREAMER_ROOT}/bin/pkg-config.exe")
if(NOT EXISTS "${_solin_pkg_config}")
    message(FATAL_ERROR "GStreamer pkg-config executable was not found")
endif()

set(PKG_CONFIG_EXECUTABLE "${_solin_pkg_config}" CACHE FILEPATH "" FORCE)
set(_solin_previous_pkg_config_path "$ENV{PKG_CONFIG_PATH}")
set(ENV{PKG_CONFIG_PATH} "${SOLIN_GSTREAMER_ROOT}/lib/pkgconfig")
find_package(PkgConfig REQUIRED)
set(SOLIN_GSTREAMER_REQUIRED_VERSION "1.28.5")
pkg_check_modules(SOLIN_GSTREAMER REQUIRED IMPORTED_TARGET
    gstreamer-1.0=${SOLIN_GSTREAMER_REQUIRED_VERSION}
    gstreamer-app-1.0=${SOLIN_GSTREAMER_REQUIRED_VERSION}
    gstreamer-base-1.0=${SOLIN_GSTREAMER_REQUIRED_VERSION}
    gstreamer-d3d11-1.0=${SOLIN_GSTREAMER_REQUIRED_VERSION}
    gstreamer-video-1.0=${SOLIN_GSTREAMER_REQUIRED_VERSION}
)
if(SOLIN_MEDIA_ENGINE_BUILD_TESTS)
    pkg_check_modules(SOLIN_GSTREAMER_RTSP_SERVER REQUIRED IMPORTED_TARGET
        gstreamer-rtsp-server-1.0=${SOLIN_GSTREAMER_REQUIRED_VERSION}
    )
endif()
set(ENV{PKG_CONFIG_PATH} "${_solin_previous_pkg_config_path}")
unset(_solin_previous_pkg_config_path)
unset(_solin_pkg_config)

if(NOT TARGET Solin::GStreamer)
    add_library(Solin::GStreamer ALIAS PkgConfig::SOLIN_GSTREAMER)
endif()
if(SOLIN_MEDIA_ENGINE_BUILD_TESTS AND NOT TARGET Solin::GStreamerRtspServer)
    add_library(Solin::GStreamerRtspServer ALIAS PkgConfig::SOLIN_GSTREAMER_RTSP_SERVER)
endif()
