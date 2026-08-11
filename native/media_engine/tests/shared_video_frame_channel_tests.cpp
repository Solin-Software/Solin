#include "solin/media_engine/shared_video_frame_channel.hpp"

#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string_view>
#include <vector>

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

#endif

} // namespace

int main() {
    test_invalid_configuration_is_rejected_before_platform_dispatch();
#ifdef _WIN32
    test_windows_channel_publishes_latest_nv12_frame();
#endif
    return failures == 0 ? 0 : 1;
}
