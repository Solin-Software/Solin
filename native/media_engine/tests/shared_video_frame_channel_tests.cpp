#include "solin/media_engine/shared_video_frame_channel.hpp"

#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace {

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (!condition) {
        ++failures;
        std::cerr << "FAILED: " << description << '\n';
    }
}

void test_invalid_configuration_is_rejected_before_platform_dispatch() {
    auto layout = solin::media_engine::packed_video_frame_layout(
        2U, 2U, solin::media_engine::VideoFramePixelFormat::nv12);
    ++layout.payload_size;
    try {
        static_cast<void>(solin::media_engine::make_shared_video_frame_publisher({
            .generation = 1U,
            .layout = layout,
        }));
        expect(false, "non-canonical channel layouts should be rejected");
    } catch (const std::invalid_argument& error) {
        expect(std::string_view{error.what()} ==
                   "shared_video_frame_channel_invalid",
               "invalid channel configurations have a stable error code");
    }
}

#ifdef _WIN32

void test_windows_channel_publishes_latest_nv12_frame() {
    const auto layout = solin::media_engine::packed_video_frame_layout(
        4U, 2U, solin::media_engine::VideoFramePixelFormat::nv12);
    auto publisher = solin::media_engine::make_shared_video_frame_publisher({
        .generation = 7U,
        .layout = layout,
    });
    auto reader = solin::media_engine::make_shared_video_frame_reader(
        publisher->native_mapping_handle(), publisher->mapping_size());
    expect(reader->configuration().generation == 7U &&
               reader->configuration().layout == layout,
           "the reader validates and exposes immutable channel metadata");
    solin::media_engine::PackedVideoFrame latest;
    expect(!reader->read_latest(latest),
           "a new output channel does not synthesize a frame");
    const auto initial_heartbeat = reader->heartbeat();
    publisher->heartbeat();
    expect(reader->heartbeat() != initial_heartbeat,
           "the producer heartbeat advances without copying a video frame");

    solin::media_engine::PackedVideoFrame first{
        .sequence = 91U,
        .presentation_timestamp_ns = 123U,
        .duration_ns = 456U,
        .produced_monotonic_ns = 789U,
        .discontinuity = true,
        .width = layout.width,
        .height = layout.height,
        .pixel_format = layout.pixel_format,
        .plane_strides = layout.plane_strides,
        .plane_offsets = layout.plane_offsets,
        .bytes = std::vector<std::uint8_t>(layout.payload_size, 0x11U),
    };
    auto second = first;
    second.presentation_timestamp_ns = 222U;
    second.discontinuity = false;
    second.bytes.assign(layout.payload_size, 0x22U);
    expect(publisher->publish(solin::media_engine::video_frame_view(first)) == 1U &&
               publisher->publish(solin::media_engine::video_frame_view(second)) == 2U,
           "the publisher owns a bounded monotonic transport sequence");

    expect(reader->read_latest(latest) && latest.sequence == 2U &&
               latest.presentation_timestamp_ns == 222U && !latest.discontinuity &&
               latest.bytes.size() == layout.payload_size &&
               latest.bytes.front() == 0x22U && latest.bytes.back() == 0x22U,
           "a slow consumer receives only the newest complete frame");
    const auto* reused_storage = latest.bytes.data();
    expect(publisher->publish(solin::media_engine::video_frame_view(first)) == 3U &&
               reader->read_latest(latest) &&
               latest.bytes.data() == reused_storage,
           "the reader reuses caller-owned payload storage");
    expect(!reader->read_latest(latest),
           "the reader does not redeliver an unchanged publication");
    bool visited = false;
    expect(
        reader->visit_current_frame(
            [&visited, &layout](const solin::media_engine::VideoFrameView& frame) {
                visited = frame.sequence == 3U &&
                          frame.pixel_format ==
                              solin::media_engine::VideoFramePixelFormat::nv12 &&
                          frame.planes[0].size() == layout.plane_offsets[1] &&
                          frame.planes[1].size() ==
                              layout.payload_size - layout.plane_offsets[1] &&
                          frame.planes[0].front() == 0x11U &&
                          frame.planes[1].back() == 0x11U;
            }) &&
            visited,
        "the reader exposes the current stable slot without an owning copy");
    const auto frame_heartbeat = reader->heartbeat();
    publisher->heartbeat();
    expect(reader->heartbeat() != frame_heartbeat && !reader->read_latest(latest),
           "liveness advances independently from frame delivery");
}

void test_cross_process_backing_file_is_consumer_read_only() {
    const auto layout = solin::media_engine::packed_video_frame_layout(
        4U, 2U, solin::media_engine::VideoFramePixelFormat::nv12);
    auto publisher =
        solin::media_engine::make_cross_process_shared_video_frame_publisher({
            .generation = 9U,
            .layout = layout,
        });
    const auto path_utf8 = publisher->backing_file_path_utf8();
    const auto required = MultiByteToWideChar(
        CP_UTF8, MB_ERR_INVALID_CHARS, path_utf8.data(),
        static_cast<int>(path_utf8.size()), nullptr, 0);
    std::wstring path(required > 0 ? static_cast<std::size_t>(required) : 0U,
                      L'\0');
    expect(required > 0 &&
               MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                                   path_utf8.data(),
                                   static_cast<int>(path_utf8.size()),
                                   path.data(), required) == required,
           "the backing-file locator is valid UTF-8");
    const auto read_handle = CreateFileW(
        path.c_str(), GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    expect(read_handle != INVALID_HANDLE_VALUE,
           "a same-user consumer can open the backing file read-only");
    if (read_handle != INVALID_HANDLE_VALUE) {
        static_cast<void>(CloseHandle(read_handle));
    }
    const auto write_handle = CreateFileW(
        path.c_str(), GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    expect(write_handle == INVALID_HANDLE_VALUE &&
               GetLastError() == ERROR_ACCESS_DENIED,
           "the published file ACL denies a consumer write handle");
    if (write_handle != INVALID_HANDLE_VALUE) {
        static_cast<void>(CloseHandle(write_handle));
    }
}

void test_zero_copy_visit_rejects_a_slot_overwritten_during_adaptation() {
    const auto layout = solin::media_engine::packed_video_frame_layout(
        4U, 2U, solin::media_engine::VideoFramePixelFormat::nv12);
    auto publisher = solin::media_engine::make_shared_video_frame_publisher({
        .generation = 10U,
        .layout = layout,
    });
    auto reader = solin::media_engine::make_shared_video_frame_reader(
        publisher->native_mapping_handle(), publisher->mapping_size());
    solin::media_engine::PackedVideoFrame frame{
        .width = layout.width,
        .height = layout.height,
        .pixel_format = layout.pixel_format,
        .plane_strides = layout.plane_strides,
        .plane_offsets = layout.plane_offsets,
        .bytes = std::vector<std::uint8_t>(layout.payload_size, 0x31U),
    };
    expect(publisher->publish(solin::media_engine::video_frame_view(frame)) ==
               1U,
           "the overwrite regression starts with one published slot");
    std::uint32_t visits = 0U;
    const auto stable = reader->visit_current_frame(
        [&publisher, &frame, &visits](
            const solin::media_engine::VideoFrameView&) {
            ++visits;
            for (std::uint32_t overwrite = 0U; overwrite < 3U; ++overwrite) {
                frame.bytes.front() =
                    static_cast<std::uint8_t>(0x40U + visits + overwrite);
                static_cast<void>(publisher->publish(
                    solin::media_engine::video_frame_view(frame)));
            }
        });
    expect(!stable && visits == 2U,
           "a slot overwritten across both attempts is never committed");
}

#endif

} // namespace

int main() {
    test_invalid_configuration_is_rejected_before_platform_dispatch();
#ifdef _WIN32
    test_windows_channel_publishes_latest_nv12_frame();
    test_cross_process_backing_file_is_consumer_read_only();
    test_zero_copy_visit_rejects_a_slot_overwritten_during_adaptation();
#endif
    return failures == 0 ? 0 : 1;
}
