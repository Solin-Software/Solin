#include "solin/media_engine/video_frame.hpp"

#include <algorithm>
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

void test_canonical_layouts() {
    using solin::media_engine::VideoFramePixelFormat;
    const auto bgra = solin::media_engine::packed_video_frame_layout(
        1920U, 1080U, VideoFramePixelFormat::bgra);
    expect(bgra.plane_count == 1U && bgra.plane_strides[0] == 7680U &&
               bgra.payload_size == 8'294'400U,
           "BGRA uses one tightly packed plane");

    const auto nv12 = solin::media_engine::packed_video_frame_layout(
        1920U, 1080U, VideoFramePixelFormat::nv12);
    expect(nv12.plane_count == 2U && nv12.plane_strides[0] == 1920U &&
               nv12.plane_strides[1] == 1920U &&
               nv12.plane_offsets[1] == 2'073'600U &&
               nv12.payload_size == 3'110'400U,
           "NV12 uses tightly packed Y and interleaved UV planes");

    const auto yuy2 = solin::media_engine::packed_video_frame_layout(
        1280U, 720U, VideoFramePixelFormat::yuy2);
    expect(yuy2.plane_count == 1U && yuy2.plane_strides[0] == 2560U &&
               yuy2.payload_size == 1'843'200U,
           "YUY2 remains available for legacy compatibility adapters");
}

void test_invalid_layouts_and_payloads_are_rejected() {
    using solin::media_engine::VideoFramePixelFormat;
    try {
        static_cast<void>(solin::media_engine::packed_video_frame_layout(
            1279U, 720U, VideoFramePixelFormat::nv12));
        expect(false, "odd-width NV12 should be rejected");
    } catch (const std::invalid_argument& error) {
        expect(std::string_view{error.what()} == "video_frame_layout_invalid",
               "invalid layouts have a stable error code");
    }

    const auto layout = solin::media_engine::packed_video_frame_layout(
        2U, 2U, VideoFramePixelFormat::bgra);
    solin::media_engine::PackedVideoFrame frame{
        .width = 2U,
        .height = 2U,
        .pixel_format = VideoFramePixelFormat::bgra,
        .plane_strides = layout.plane_strides,
        .plane_offsets = layout.plane_offsets,
        .bytes = std::vector<std::uint8_t>(15U),
    };
    try {
        solin::media_engine::validate_packed_video_frame(frame, layout);
        expect(false, "truncated packed frames should be rejected");
    } catch (const std::invalid_argument& error) {
        expect(std::string_view{error.what()} == "video_frame_payload_invalid",
               "invalid payloads have a stable error code");
    }
}

void test_frame_views_copy_padded_and_inverted_planes_without_an_intermediate() {
    using solin::media_engine::VideoFramePixelFormat;
    std::vector<std::uint8_t> padded(20U, 0xEEU);
    std::fill_n(padded.begin(), 8U, static_cast<std::uint8_t>(0x11U));
    std::fill_n(padded.begin() + 12U, 8U,
                static_cast<std::uint8_t>(0x22U));
    solin::media_engine::VideoFrameView view{
        .sequence = 7U,
        .width = 2U,
        .height = 2U,
        .pixel_format = VideoFramePixelFormat::bgra,
        .planes = {std::span<const std::uint8_t>{padded}, {}},
        .plane_strides = {12, 0},
    };

    const auto copied = solin::media_engine::copy_video_frame(view);
    expect(copied.sequence == 7U && copied.bytes.size() == 16U &&
               std::all_of(copied.bytes.begin(), copied.bytes.begin() + 8U,
                           [](const auto value) { return value == 0x11U; }) &&
               std::all_of(copied.bytes.begin() + 8U, copied.bytes.end(),
                           [](const auto value) { return value == 0x22U; }),
           "frame views remove source padding while preserving row order");

    view.plane_strides[0] = -12;
    const auto inverted = solin::media_engine::copy_video_frame(view);
    expect(inverted.bytes.front() == 0x22U && inverted.bytes.back() == 0x11U,
           "negative-stride frame views are normalized without a staging frame");
}

void test_tightly_packed_nv12_planes_copy_as_one_payload_per_plane() {
    using solin::media_engine::VideoFramePixelFormat;
    const std::vector<std::uint8_t> y{
        1U, 2U, 3U, 4U, 5U, 6U, 7U, 8U,
    };
    const std::vector<std::uint8_t> uv{9U, 10U, 11U, 12U};
    const solin::media_engine::VideoFrameView view{
        .width = 4U,
        .height = 2U,
        .pixel_format = VideoFramePixelFormat::nv12,
        .planes = {std::span<const std::uint8_t>{y},
                   std::span<const std::uint8_t>{uv}},
        .plane_strides = {4, 4},
    };
    const auto copied = solin::media_engine::copy_video_frame(view);
    const std::vector<std::uint8_t> expected{
        1U, 2U, 3U, 4U, 5U, 6U, 7U, 8U, 9U, 10U, 11U, 12U,
    };
    expect(copied.bytes == expected,
           "tightly packed NV12 copies both contiguous planes exactly");
}

} // namespace

int main() {
    test_canonical_layouts();
    test_invalid_layouts_and_payloads_are_rejected();
    test_frame_views_copy_padded_and_inverted_planes_without_an_intermediate();
    test_tightly_packed_nv12_planes_copy_as_one_payload_per_plane();
    return failures == 0 ? 0 : 1;
}
