cmake_minimum_required(VERSION 3.25)
project(solin_media_playback VERSION 1.0.0 LANGUAGES C)

set(SOLIN_OBS_ROOT "" CACHE PATH "Verified and patched OBS source subset")
set(SOLIN_OBS_RUNTIME "" CACHE PATH "Installed pylibobs platform runtime")
set(SOLIN_FFMPEG_ROOT "" CACHE PATH "Matching OBS FFmpeg SDK")
foreach(required SOLIN_OBS_ROOT SOLIN_OBS_RUNTIME SOLIN_FFMPEG_ROOT)
    if(NOT IS_DIRECTORY "${${required}}")
        message(FATAL_ERROR "${required} must identify an existing directory")
    endif()
endforeach()

set(playback "${SOLIN_OBS_ROOT}/shared/media-playback/media-playback")
add_library(solin-media-runtime INTERFACE)
add_library(solin-media-source MODULE
    module.c rate_tempo.c
    "${SOLIN_OBS_ROOT}/plugins/obs-ffmpeg/obs-ffmpeg-source.c"
    "${playback}/media-playback.c" "${playback}/media.c"
    "${playback}/decode.c" "${playback}/cache.c")
set_target_properties(solin-media-source PROPERTIES PREFIX "" C_STANDARD 17
    C_STANDARD_REQUIRED YES C_VISIBILITY_PRESET hidden BUILD_WITH_INSTALL_RPATH YES)
if(APPLE)
    set_target_properties(solin-media-source PROPERTIES SUFFIX ".dylib")
endif()
target_include_directories(solin-media-runtime SYSTEM INTERFACE
    "${CMAKE_CURRENT_SOURCE_DIR}"
    "${SOLIN_OBS_ROOT}/libobs" "${SOLIN_OBS_ROOT}/generated"
    "${SOLIN_OBS_ROOT}/shared/media-playback"
    "${SOLIN_FFMPEG_ROOT}/include")

# Headers are upstream public ABI declarations; only these build-time config
# values are required. No private OBS structures or library replacements.
file(MAKE_DIRECTORY "${SOLIN_OBS_ROOT}/generated")
file(WRITE "${SOLIN_OBS_ROOT}/generated/obsconfig.h"
    "#pragma once\n#define OBS_RELEASE_CANDIDATE 0\n#define OBS_BETA 0\n")

function(runtime_library target filename)
    set(binary "${SOLIN_OBS_RUNTIME}/${filename}")
    if(NOT EXISTS "${binary}")
        message(FATAL_ERROR "Installed OBS runtime is missing ${binary}")
    endif()
    if(WIN32)
        # The wheel ships DLLs, not import libraries. Derive import libraries
        # from exactly those exports with the selected MSVC toolchain.
        execute_process(COMMAND "${CMAKE_LINKER}" /dump /exports "${binary}"
            OUTPUT_VARIABLE exports COMMAND_ERROR_IS_FATAL ANY)
        string(REPLACE "\n" ";" lines "${exports}")
        set(definition "LIBRARY ${filename}\nEXPORTS\n")
        set(count 0)
        foreach(line IN LISTS lines)
            if(line MATCHES "^[ \t]+[0-9]+[ \t]+[0-9A-Fa-f]+[ \t]+[0-9A-Fa-f]+[ \t]+([^ \t\r\n]+)")
                string(APPEND definition "${CMAKE_MATCH_1}\n")
                math(EXPR count "${count}+1")
            endif()
        endforeach()
        if(count EQUAL 0)
            message(FATAL_ERROR "No DLL exports found in ${binary}")
        endif()
        set(import "${CMAKE_CURRENT_BINARY_DIR}/${target}.lib")
        file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/${target}.def" "${definition}")
        execute_process(COMMAND "${CMAKE_AR}" /nologo /machine:x64
            "/def:${CMAKE_CURRENT_BINARY_DIR}/${target}.def" "/out:${import}"
            COMMAND_ERROR_IS_FATAL ANY OUTPUT_QUIET)
        target_link_libraries(solin-media-runtime INTERFACE "${import}")
    else()
        target_link_libraries(solin-media-runtime INTERFACE "${binary}")
    endif()
endfunction()

if(WIN32)
    if(NOT MSVC OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
        message(FATAL_ERROR "Windows playback builds require the MSVC x64 toolchain")
    endif()
    target_include_directories(solin-media-runtime SYSTEM INTERFACE "${SOLIN_OBS_ROOT}/deps/w32-pthreads")
    target_compile_options(solin-media-runtime INTERFACE /utf-8 /W3)
    target_link_options(solin-media-source PRIVATE /Brepro)
    target_compile_definitions(solin-media-runtime INTERFACE
        WIN32_LEAN_AND_MEAN NOMINMAX _CRT_SECURE_NO_WARNINGS)
    set_property(TARGET solin-media-source PROPERTY MSVC_RUNTIME_LIBRARY
        "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")
    runtime_library(obs obs.dll)
    runtime_library(pthreads w32-pthreads.dll)
    foreach(pair avcodec-62 avdevice-62 avfilter-11 avformat-62 avutil-60 swscale-9)
        runtime_library(${pair} "${pair}.dll")
    endforeach()
elseif(APPLE)
    runtime_library(obs "Frameworks/libobs.framework/Versions/A/libobs")
    foreach(pair avcodec avdevice avfilter avformat avutil swscale)
        runtime_library(${pair} "Frameworks/lib${pair}.dylib")
    endforeach()
    set_target_properties(solin-media-source PROPERTIES
        BUILD_RPATH "@loader_path/../Frameworks" INSTALL_RPATH "@loader_path/../Frameworks")
else()
    find_package(PkgConfig REQUIRED)
    find_path(SIMDE_INCLUDE_DIR simde/x86/sse2.h REQUIRED)
    target_include_directories(solin-media-runtime SYSTEM INTERFACE
        "${SIMDE_INCLUDE_DIR}")
    runtime_library(obs libobs.so.30)
    # The pinned Linux wheel obtains FFmpeg from the Ubuntu 26.04 runtime.
    # Reuse its installed providers; packaging stages/relocates the same graph.
    pkg_check_modules(FFMPEG REQUIRED IMPORTED_TARGET
        libavcodec>=62 libavdevice>=62 libavfilter>=11 libavformat>=62
        libavutil>=60 libswscale>=9)
    target_link_libraries(solin-media-runtime INTERFACE PkgConfig::FFMPEG m)
    target_link_options(solin-media-source PRIVATE "LINKER:--no-undefined")
    set_target_properties(solin-media-source PROPERTIES BUILD_RPATH "$ORIGIN/../.."
        INSTALL_RPATH "$ORIGIN/../..")
endif()
find_package(Threads REQUIRED)
target_link_libraries(solin-media-runtime INTERFACE Threads::Threads)
target_link_libraries(solin-media-source PRIVATE solin-media-runtime)
if(WIN32)
    set(module_destination "core/solin-media-source")
elseif(APPLE)
    set(module_destination "obs-plugins")
else()
    set(module_destination "obs-modules/core")
endif()
install(TARGETS solin-media-source
    LIBRARY DESTINATION "${module_destination}" RUNTIME DESTINATION "${module_destination}")

# Compile the actual patched seek function with controlled FFmpeg/decoder
# boundaries. This exercises failure handling without a network or GPU.
file(READ "${playback}/media.c" media_source)
string(FIND "${media_source}" "static void seek_to(" seek_start)
string(FIND "${media_source}" "bool mp_media_reset(" seek_end)
if(seek_start LESS 0 OR seek_end LESS seek_start)
    message(FATAL_ERROR "Pinned seek function boundaries changed")
endif()
math(EXPR seek_length "${seek_end}-${seek_start}")
string(SUBSTRING "${media_source}" ${seek_start} ${seek_length} seek_function)
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/seek-under-test.h" "${seek_function}")
enable_testing()
add_executable(solin-media-tempo-tests tests/rate_tempo_tests.c rate_tempo.c)
target_link_libraries(solin-media-tempo-tests PRIVATE solin-media-runtime)
set_target_properties(solin-media-tempo-tests PROPERTIES C_STANDARD 17 C_STANDARD_REQUIRED YES
    BUILD_RPATH "${SOLIN_OBS_RUNTIME};${SOLIN_OBS_RUNTIME}/Frameworks")
add_test(NAME media_tempo_contract COMMAND solin-media-tempo-tests)
if(WIN32)
    set_tests_properties(media_tempo_contract PROPERTIES
        ENVIRONMENT_MODIFICATION "PATH=path_list_prepend:${SOLIN_OBS_RUNTIME}")
endif()
add_executable(solin-media-seek-tests tests/seek_contract_tests.c)
target_include_directories(solin-media-seek-tests PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")
set_target_properties(solin-media-seek-tests PROPERTIES C_STANDARD 17 C_STANDARD_REQUIRED YES)
add_test(NAME media_seek_contract COMMAND solin-media-seek-tests)

string(FIND "${media_source}" "static int interrupt_callback(" interrupt_start)
string(FIND "${media_source}" "static void reset_ts(" interrupt_end)
if(interrupt_start LESS 0 OR interrupt_end LESS interrupt_start)
    message(FATAL_ERROR "Pinned interrupt/init function boundaries changed")
endif()
math(EXPR interrupt_length "${interrupt_end}-${interrupt_start}")
string(SUBSTRING "${media_source}" ${interrupt_start} ${interrupt_length} interrupt_functions)
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/interrupt-under-test.h" "${interrupt_functions}")
add_executable(solin-media-interrupt-tests tests/interrupt_contract_tests.c)
target_include_directories(solin-media-interrupt-tests PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")
set_target_properties(solin-media-interrupt-tests PROPERTIES C_STANDARD 17 C_STANDARD_REQUIRED YES)
add_test(NAME media_interrupt_contract COMMAND solin-media-interrupt-tests)

function(append_test_span source begin end output)
    file(READ "${source}" content)
    string(FIND "${content}" "${begin}" first)
    string(FIND "${content}" "${end}" last)
    if(first LESS 0 OR last LESS first)
        message(FATAL_ERROR "Pinned terminal function boundaries changed: ${begin}")
    endif()
    math(EXPR length "${last}-${first}")
    string(SUBSTRING "${content}" ${first} ${length} body)
    file(APPEND "${output}" "${body}\n")
endfunction()

append_test_span("${playback}/media.c" "static uint64_t mp_media_read_budget(mp_media_t *m)\n{"
    "static inline int64_t mp_media_get_next_min_pts(" "${CMAKE_CURRENT_BINARY_DIR}/interrupt-under-test.h")

set(terminal_header "${CMAKE_CURRENT_BINARY_DIR}/terminal-under-test.h")
file(WRITE "${terminal_header}" "")
set(source "${SOLIN_OBS_ROOT}/plugins/obs-ffmpeg/obs-ffmpeg-source.c")
append_test_span("${source}" "static void stop_reconnect_thread(" "static enum obs_media_state get_media_state(" "${terminal_header}")
append_test_span("${source}" "static void media_buffering(" "static void ffmpeg_source_open(" "${terminal_header}")
append_test_span("${source}" "static void ffmpeg_source_start(" "static void *ffmpeg_source_reconnect(" "${terminal_header}")
append_test_span("${source}" "static void *ffmpeg_source_reconnect(" "#define RIST_PROTO" "${terminal_header}")
append_test_span("${source}" "static void ffmpeg_source_play_pause(" "static int64_t ffmpeg_source_get_duration(" "${terminal_header}")
append_test_span("${playback}/media.c" "static int mp_media_next_packet(" "static inline bool mp_media_ready_to_start(" "${terminal_header}")
append_test_span("${playback}/media.c" "static void *mp_media_thread_start(" "static inline bool mp_media_init_internal(" "${terminal_header}")
append_test_span("${playback}/media.c" "bool mp_media_eof(" "static int interrupt_callback(" "${terminal_header}")
append_test_span("${playback}/media.c" "void mp_media_play(" "void mp_media_play_pause(" "${terminal_header}")
append_test_span("${playback}/cache.c" "static void *mp_cache_thread_start(" "static void fill_video(" "${terminal_header}")
add_executable(solin-media-terminal-tests tests/terminal_contract_tests.c)
target_include_directories(solin-media-terminal-tests PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")
set_target_properties(solin-media-terminal-tests PROPERTIES C_STANDARD 17 C_STANDARD_REQUIRED YES)
add_test(NAME media_terminal_contract COMMAND solin-media-terminal-tests)
foreach(contract stop_resources stop_without_media stop_entered_reconnect stopped_tick
        stop_then_play failed_live_reconnect)
    add_test(NAME media_terminal_${contract} COMMAND solin-media-terminal-tests ${contract})
endforeach()
