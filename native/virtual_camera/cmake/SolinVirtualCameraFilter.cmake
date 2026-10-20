include_guard(GLOBAL)

include(FetchContent)

function(solin_add_virtual_camera_filter)
    if(NOT WIN32 OR NOT MSVC)
        message(FATAL_ERROR
            "The Solin DirectShow virtual-camera filter requires MSVC on Windows"
        )
    endif()

    FetchContent_Declare(
        solin_directshow_baseclasses_source
        URL https://github.com/microsoft/Windows-classic-samples/archive/d59e5f1dc9c768615e4e1ab1f0f009e6a3ed747c.tar.gz
        URL_HASH SHA256=21ab0b0cfdad99f83a0b1bf866441eab3d5d2bf8159f6a516d6ea8c7655ad65b
        DOWNLOAD_EXTRACT_TIMESTAMP TRUE
    )
    FetchContent_Declare(
        solin_libyuv_source
        # GitHub mirror archive of the exact upstream libyuv revision. Gitiles
        # archives are gzip-time-dependent and therefore cannot be SHA-256 pinned.
        URL https://github.com/lemenkov/libyuv/archive/eb6e7bb63738e29efd82ea3cf2a115238a89fa51.tar.gz
        URL_HASH SHA256=0442ca69a608985bd8c220d9decfa9eba8331a2791957fcd2bec94a654b10562
        DOWNLOAD_EXTRACT_TIMESTAMP TRUE
    )
    if(POLICY CMP0169)
        cmake_policy(SET CMP0169 OLD)
    endif()
    FetchContent_GetProperties(solin_directshow_baseclasses_source)
    if(NOT solin_directshow_baseclasses_source_POPULATED)
        FetchContent_Populate(solin_directshow_baseclasses_source)
    endif()
    FetchContent_GetProperties(solin_libyuv_source)
    if(NOT solin_libyuv_source_POPULATED)
        FetchContent_Populate(solin_libyuv_source)
    endif()

    set(baseclasses_dir
        "${solin_directshow_baseclasses_source_SOURCE_DIR}/Samples/Win7Samples/multimedia/directshow/baseclasses"
    )
    set(baseclasses_sources
        amextra.cpp amfilter.cpp amvideo.cpp arithutil.cpp combase.cpp cprop.cpp
        ctlutil.cpp ddmm.cpp dllentry.cpp dllsetup.cpp mtype.cpp outputq.cpp
        perflog.cpp pstream.cpp pullpin.cpp refclock.cpp renbase.cpp schedule.cpp
        seekpt.cpp source.cpp strmctl.cpp sysclock.cpp transfrm.cpp transip.cpp
        videoctl.cpp vtrans.cpp winctrl.cpp winutil.cpp wxdebug.cpp wxlist.cpp
        wxutil.cpp
    )
    list(TRANSFORM baseclasses_sources PREPEND "${baseclasses_dir}/")
    add_library(solin_directshow_baseclasses STATIC ${baseclasses_sources})
    target_include_directories(solin_directshow_baseclasses PUBLIC "${baseclasses_dir}")
    target_compile_definitions(solin_directshow_baseclasses
        PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN
                WINVER=0x0A00 _WIN32_WINNT=0x0A00
    )
    target_compile_options(solin_directshow_baseclasses
        PRIVATE /W3 /wd4100 /wd4244 /wd4267 /wd4596 /wd4996
                /permissive /Zc:strictStrings- /EHsc
    )
    set_property(TARGET solin_directshow_baseclasses PROPERTY
        MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
    )

    file(GLOB libyuv_sources CONFIGURE_DEPENDS
        "${solin_libyuv_source_SOURCE_DIR}/source/*.cc"
    )
    add_library(solin_virtual_camera_libyuv STATIC ${libyuv_sources})
    target_include_directories(solin_virtual_camera_libyuv
        PUBLIC "${solin_libyuv_source_SOURCE_DIR}/include"
    )
    target_compile_definitions(solin_virtual_camera_libyuv
        PRIVATE LIBYUV_DISABLE_JPEG
    )
    target_compile_options(solin_virtual_camera_libyuv
        PRIVATE /W3 /wd4100 /wd4244 /wd4267 /wd4701 /permissive- /EHsc
    )
    set_property(TARGET solin_virtual_camera_libyuv PROPERTY
        MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
    )

    add_library(solin_virtual_camera_frame_adapter STATIC
        windows/directshow_virtual_camera/frame_adapter.cpp
        windows/directshow_virtual_camera/media_profiles.cpp
    )
    target_include_directories(solin_virtual_camera_frame_adapter
        PUBLIC
            "${CMAKE_CURRENT_SOURCE_DIR}/include"
            "${CMAKE_CURRENT_SOURCE_DIR}/windows/directshow_virtual_camera"
    )
    target_compile_features(solin_virtual_camera_frame_adapter PUBLIC cxx_std_20)
    target_compile_definitions(solin_virtual_camera_frame_adapter
        PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX
                WINVER=0x0A00 _WIN32_WINNT=0x0A00
    )
    target_compile_options(solin_virtual_camera_frame_adapter
        PRIVATE /W4 /WX /permissive- /EHsc
    )
    target_link_libraries(solin_virtual_camera_frame_adapter
        PUBLIC solin_virtual_camera_libyuv
        PRIVATE gdi32 user32
    )
    set_property(TARGET solin_virtual_camera_frame_adapter PROPERTY
        MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
    )

    set(filter_targets solin-virtual-camera)
    if(SOLIN_VIRTUAL_CAMERA_BUILD_TESTS)
        list(APPEND filter_targets solin-virtual-camera-test)
    endif()
    foreach(filter_target IN LISTS filter_targets)
        add_library(${filter_target} SHARED
            windows/directshow_virtual_camera/broker_client.cpp
            windows/directshow_virtual_camera/filter.cpp
            windows/directshow_virtual_camera/module.cpp
            windows/directshow_virtual_camera/standby_resources.rc
            windows/directshow_virtual_camera/solin_virtual_camera.def
        )
        target_include_directories(${filter_target}
            PRIVATE
                "${CMAKE_CURRENT_SOURCE_DIR}/include"
                "${CMAKE_CURRENT_SOURCE_DIR}/windows/directshow_virtual_camera"
        )
        target_compile_features(${filter_target} PRIVATE cxx_std_20)
        target_compile_definitions(${filter_target}
            PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX
                    WINVER=0x0A00 _WIN32_WINNT=0x0A00
        )
        target_compile_options(${filter_target}
            PRIVATE /W4 /WX /wd4324 /wd4596 /permissive /Zc:strictStrings- /EHsc
        )
        target_link_options(${filter_target}
            PRIVATE /DYNAMICBASE /NXCOMPAT /CETCOMPAT /GUARD:CF
        )
        target_link_libraries(${filter_target}
            PRIVATE
                solin_directshow_baseclasses
                solin_virtual_camera_frame_adapter
                solin_virtual_camera_transport
                solin_virtual_camera_libyuv
                advapi32
                bcrypt
                gdi32
                ole32
                oleaut32
                quartz
                strmiids
                user32
                uuid
                winmm
        )
        set_property(TARGET ${filter_target} PROPERTY
            MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
        )
        set_target_properties(${filter_target} PROPERTIES
            PREFIX ""
            OUTPUT_NAME "${filter_target}"
        )

    endforeach()
    if(SOLIN_VIRTUAL_CAMERA_BUILD_TESTS)
        target_compile_definitions(solin-virtual-camera-test
            PRIVATE SOLIN_VIRTUAL_CAMERA_TEST_ENDPOINT
        )
        target_include_directories(solin-virtual-camera-test
            PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/tests"
        )
        set_target_properties(solin-virtual-camera-test PROPERTIES
            OUTPUT_NAME "solin-virtual-camera"
            RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/test"
            ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/test"
        )
    endif()
    if(CMAKE_SIZEOF_VOID_P EQUAL 8)
        set(filter_architecture x64)
    else()
        set(filter_architecture x86)
    endif()
    install(TARGETS solin-virtual-camera
        RUNTIME DESTINATION "virtual-camera/${filter_architecture}"
    )
    install(FILES
        "${solin_directshow_baseclasses_source_SOURCE_DIR}/LICENSE"
        DESTINATION share/licenses/solin-virtual-camera/directshow-baseclasses
    )
    install(FILES
        "${solin_libyuv_source_SOURCE_DIR}/LICENSE"
        DESTINATION share/licenses/solin-virtual-camera/libyuv
    )

    if(SOLIN_VIRTUAL_CAMERA_BUILD_TESTS)
        enable_testing()
        add_executable(solin-virtual-camera-broker-probe
            tests/broker_frame_probe.cpp
            windows/directshow_virtual_camera/broker_client.cpp
        )
        target_include_directories(solin-virtual-camera-broker-probe
            PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/windows/directshow_virtual_camera"
        )
        target_compile_definitions(solin-virtual-camera-broker-probe
            PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX
                    WINVER=0x0A00 _WIN32_WINNT=0x0A00
        )
        target_link_libraries(solin-virtual-camera-broker-probe
            PRIVATE solin_virtual_camera_transport bcrypt
        )
        solin_camera_target(solin-virtual-camera-broker-probe)
        add_executable(solin-virtual-camera-filter-tests
            tests/directshow_virtual_camera_tests.cpp
        )
        target_include_directories(solin-virtual-camera-filter-tests
            PRIVATE
                "${CMAKE_CURRENT_SOURCE_DIR}/include"
                "${CMAKE_CURRENT_SOURCE_DIR}/windows/directshow_virtual_camera"
                "${baseclasses_dir}"
        )
        target_compile_features(solin-virtual-camera-filter-tests PRIVATE cxx_std_20)
        target_compile_definitions(solin-virtual-camera-filter-tests
            PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX
                    WINVER=0x0A00 _WIN32_WINNT=0x0A00
        )
        target_compile_options(solin-virtual-camera-filter-tests
            PRIVATE /W4 /WX /wd4596 /permissive /Zc:strictStrings- /EHsc
        )
        target_link_libraries(solin-virtual-camera-filter-tests
            PRIVATE
                solin_directshow_baseclasses
                solin_virtual_camera_transport
                solin_virtual_camera_frame_adapter
                ole32
                oleaut32
                strmiids
                uuid
                winmm
        )
        set_property(TARGET solin-virtual-camera-filter-tests PROPERTY
            MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
        )
        add_dependencies(solin-virtual-camera-filter-tests solin-virtual-camera-test)
        add_test(NAME directshow-virtual-camera-filter
            COMMAND solin-virtual-camera-filter-tests
                    $<TARGET_FILE:solin-virtual-camera-test>
        )
        add_test(NAME directshow-virtual-camera-filter-backpressure
            COMMAND solin-virtual-camera-filter-tests
                    $<TARGET_FILE:solin-virtual-camera-test> --slow-consumer
        )
        foreach(freshness_format IN ITEMS nv12 yuy2)
            if(freshness_format STREQUAL "nv12")
                set(freshness_argument --frame-freshness)
            else()
                set(freshness_argument --frame-freshness-yuy2)
            endif()
            add_test(NAME directshow-virtual-camera-freshness-${freshness_format}
                COMMAND solin-virtual-camera-filter-tests
                        $<TARGET_FILE:solin-virtual-camera-test> ${freshness_argument}
            )
        endforeach()
        add_test(NAME directshow-virtual-camera-stale-reconnect
            COMMAND solin-virtual-camera-filter-tests
                    $<TARGET_FILE:solin-virtual-camera-test> --stale-reconnect
        )
        add_executable(solin-virtual-camera-frame-cadence-tests
            tests/directshow_frame_cadence_tests.cpp
        )
        target_include_directories(solin-virtual-camera-frame-cadence-tests
            PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/windows/directshow_virtual_camera"
        )
        target_compile_features(solin-virtual-camera-frame-cadence-tests
            PRIVATE cxx_std_20
        )
        target_compile_options(solin-virtual-camera-frame-cadence-tests
            PRIVATE /W4 /WX /permissive- /EHsc
        )
        set_property(TARGET solin-virtual-camera-frame-cadence-tests PROPERTY
            MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
        )
        add_test(NAME directshow-virtual-camera-frame-cadence
            COMMAND solin-virtual-camera-frame-cadence-tests
        )
        add_executable(solin-virtual-camera-frame-adapter-tests
            tests/directshow_frame_adapter_tests.cpp
        )
        target_link_libraries(solin-virtual-camera-frame-adapter-tests
            PRIVATE solin_virtual_camera_frame_adapter
        )
        target_compile_features(solin-virtual-camera-frame-adapter-tests
            PRIVATE cxx_std_20
        )
        target_compile_options(solin-virtual-camera-frame-adapter-tests
            PRIVATE /W4 /WX /permissive- /EHsc
        )
        set_property(TARGET solin-virtual-camera-frame-adapter-tests PROPERTY
            MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
        )
        add_test(NAME directshow-virtual-camera-frame-adapter
            COMMAND solin-virtual-camera-frame-adapter-tests
        )
        add_executable(solin-virtual-camera-frame-adapter-benchmark
            tests/directshow_frame_adapter_benchmark.cpp
        )
        target_link_libraries(solin-virtual-camera-frame-adapter-benchmark
            PRIVATE solin_virtual_camera_frame_adapter
        )
        target_compile_features(solin-virtual-camera-frame-adapter-benchmark
            PRIVATE cxx_std_20
        )
        target_compile_options(solin-virtual-camera-frame-adapter-benchmark
            PRIVATE /W4 /WX /permissive- /EHsc
        )
        set_property(TARGET solin-virtual-camera-frame-adapter-benchmark PROPERTY
            MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
        )
        add_test(NAME directshow-virtual-camera-frame-adapter-benchmark
            COMMAND solin-virtual-camera-frame-adapter-benchmark
        )
        set_tests_properties(
            directshow-virtual-camera-frame-adapter-benchmark
            PROPERTIES
                CONFIGURATIONS "Release;RelWithDebInfo"
                LABELS performance
                TIMEOUT 60
        )
    endif()
endfunction()
