#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <vector>

namespace solin::media_engine {

enum class VideoFramePixelFormat : std::uint32_t {
    bgra = 1U,
    nv12 = 2U,
    yuy2 = 3U,
};

struct PackedVideoFrame final {
    std::uint64_t sequence{0U};
    std::uint64_t presentation_timestamp_ns{0U};
    std::uint64_t duration_ns{0U};
    std::uint64_t produced_monotonic_ns{0U};
    bool discontinuity{false};
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    VideoFramePixelFormat pixel_format{VideoFramePixelFormat::bgra};
    std::array<std::uint32_t, 2U> plane_strides{};
    std::array<std::uint64_t, 2U> plane_offsets{};
    std::vector<std::uint8_t> bytes{};
};

// Borrowed planes are valid only for the synchronous call that receives the
// view. Consumers must copy into their final destination or create an owning
// PackedVideoFrame before returning.
struct VideoFrameView final {
    std::uint64_t sequence{0U};
    std::uint64_t presentation_timestamp_ns{0U};
    std::uint64_t duration_ns{0U};
    std::uint64_t produced_monotonic_ns{0U};
    bool discontinuity{false};
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    VideoFramePixelFormat pixel_format{VideoFramePixelFormat::bgra};
    std::array<std::span<const std::uint8_t>, 2U> planes{};
    std::array<std::int32_t, 2U> plane_strides{};
};

struct PackedVideoFrameLayout final {
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    VideoFramePixelFormat pixel_format{VideoFramePixelFormat::bgra};
    std::array<std::uint32_t, 2U> plane_strides{};
    std::array<std::uint64_t, 2U> plane_offsets{};
    std::uint32_t plane_count{0U};
    std::uint64_t payload_size{0U};

    bool operator==(const PackedVideoFrameLayout&) const = default;
};

[[nodiscard]] PackedVideoFrameLayout
packed_video_frame_layout(std::uint32_t width, std::uint32_t height,
                          VideoFramePixelFormat pixel_format);
void validate_packed_video_frame(const PackedVideoFrame& frame,
                                 const PackedVideoFrameLayout& expected_layout);
[[nodiscard]] VideoFrameView video_frame_view(const PackedVideoFrame& frame);
void validate_video_frame_view(const VideoFrameView& frame,
                               const PackedVideoFrameLayout& expected_layout);
void copy_video_frame_pixels(const VideoFrameView& frame,
                             std::span<std::uint8_t> destination);
[[nodiscard]] PackedVideoFrame copy_video_frame(const VideoFrameView& frame);

} // namespace solin::media_engine
