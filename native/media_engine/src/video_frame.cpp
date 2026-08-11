#include "solin/media_engine/video_frame.hpp"

#include <cstring>
#include <limits>
#include <stdexcept>

namespace solin::media_engine {
namespace {

constexpr std::uint32_t kMaximumDimension = 3'840U;
constexpr std::uint64_t kMaximumPixels = 3'840ULL * 2'160ULL;

[[nodiscard]] std::uint64_t checked_multiply(const std::uint64_t left,
                                             const std::uint64_t right) {
    if (right != 0U && left > (std::numeric_limits<std::uint64_t>::max)() / right) {
        throw std::invalid_argument("video_frame_layout_invalid");
    }
    return left * right;
}

[[nodiscard]] std::uint64_t absolute_stride(const std::int32_t stride) noexcept {
    return stride < 0 ? static_cast<std::uint64_t>(
                            -static_cast<std::int64_t>(stride))
                      : static_cast<std::uint64_t>(stride);
}

[[nodiscard]] std::uint32_t plane_rows(const PackedVideoFrameLayout& layout,
                                       const std::uint32_t plane) noexcept {
    return plane == 1U && layout.pixel_format == VideoFramePixelFormat::nv12
               ? layout.height / 2U
               : layout.height;
}

} // namespace

PackedVideoFrameLayout packed_video_frame_layout(
    const std::uint32_t width, const std::uint32_t height,
    const VideoFramePixelFormat pixel_format) {
    const auto pixels = checked_multiply(width, height);
    if (width == 0U || height == 0U || width > kMaximumDimension ||
        height > kMaximumDimension || pixels > kMaximumPixels) {
        throw std::invalid_argument("video_frame_layout_invalid");
    }

    PackedVideoFrameLayout result{
        .width = width,
        .height = height,
        .pixel_format = pixel_format,
    };
    switch (pixel_format) {
    case VideoFramePixelFormat::bgra:
        result.plane_strides = {width * 4U, 0U};
        result.plane_offsets = {0U, 0U};
        result.plane_count = 1U;
        result.payload_size = checked_multiply(pixels, 4U);
        break;
    case VideoFramePixelFormat::nv12:
        if ((width % 2U) != 0U || (height % 2U) != 0U) {
            throw std::invalid_argument("video_frame_layout_invalid");
        }
        result.plane_strides = {width, width};
        result.plane_offsets = {0U, pixels};
        result.plane_count = 2U;
        result.payload_size = pixels + pixels / 2U;
        break;
    case VideoFramePixelFormat::yuy2:
        if ((width % 2U) != 0U) {
            throw std::invalid_argument("video_frame_layout_invalid");
        }
        result.plane_strides = {width * 2U, 0U};
        result.plane_offsets = {0U, 0U};
        result.plane_count = 1U;
        result.payload_size = checked_multiply(pixels, 2U);
        break;
    default:
        throw std::invalid_argument("video_frame_layout_invalid");
    }
    return result;
}

void validate_packed_video_frame(const PackedVideoFrame& frame,
                                 const PackedVideoFrameLayout& expected_layout) {
    const auto actual_layout =
        packed_video_frame_layout(frame.width, frame.height, frame.pixel_format);
    if (actual_layout != expected_layout || frame.plane_strides != actual_layout.plane_strides ||
        frame.plane_offsets != actual_layout.plane_offsets || frame.bytes.size() !=
                                                                actual_layout.payload_size) {
        throw std::invalid_argument("video_frame_payload_invalid");
    }
}

VideoFrameView video_frame_view(const PackedVideoFrame& frame) {
    const auto layout =
        packed_video_frame_layout(frame.width, frame.height, frame.pixel_format);
    validate_packed_video_frame(frame, layout);
    VideoFrameView result{
        .sequence = frame.sequence,
        .presentation_timestamp_ns = frame.presentation_timestamp_ns,
        .duration_ns = frame.duration_ns,
        .produced_monotonic_ns = frame.produced_monotonic_ns,
        .discontinuity = frame.discontinuity,
        .width = frame.width,
        .height = frame.height,
        .pixel_format = frame.pixel_format,
        .plane_strides = {
            static_cast<std::int32_t>(frame.plane_strides[0]),
            static_cast<std::int32_t>(frame.plane_strides[1]),
        },
    };
    for (std::uint32_t plane = 0U; plane < layout.plane_count; ++plane) {
        const auto begin = static_cast<std::size_t>(layout.plane_offsets[plane]);
        const auto end = plane + 1U < layout.plane_count
                             ? static_cast<std::size_t>(layout.plane_offsets[plane + 1U])
                             : frame.bytes.size();
        result.planes[plane] =
            std::span<const std::uint8_t>{frame.bytes}.subspan(begin, end - begin);
    }
    return result;
}

void validate_video_frame_view(const VideoFrameView& frame,
                               const PackedVideoFrameLayout& expected_layout) {
    const auto actual_layout =
        packed_video_frame_layout(frame.width, frame.height, frame.pixel_format);
    if (actual_layout != expected_layout) {
        throw std::invalid_argument("video_frame_payload_invalid");
    }
    for (std::uint32_t plane = 0U; plane < actual_layout.plane_count; ++plane) {
        const auto stride = absolute_stride(frame.plane_strides[plane]);
        const auto row_bytes = actual_layout.plane_strides[plane];
        const auto rows = plane_rows(actual_layout, plane);
        const auto required_size =
            rows == 0U ? 0U : checked_multiply(stride, rows - 1U) + row_bytes;
        if (stride < row_bytes || frame.planes[plane].data() == nullptr ||
            frame.planes[plane].size() < required_size) {
            throw std::invalid_argument("video_frame_payload_invalid");
        }
    }
}

void copy_video_frame_pixels(const VideoFrameView& frame,
                             const std::span<std::uint8_t> destination) {
    const auto layout =
        packed_video_frame_layout(frame.width, frame.height, frame.pixel_format);
    validate_video_frame_view(frame, layout);
    if (destination.size() != layout.payload_size) {
        throw std::invalid_argument("video_frame_payload_invalid");
    }
    for (std::uint32_t plane = 0U; plane < layout.plane_count; ++plane) {
        const auto stride = absolute_stride(frame.plane_strides[plane]);
        const auto row_bytes = layout.plane_strides[plane];
        const auto rows = plane_rows(layout, plane);
        auto* output = destination.data() + layout.plane_offsets[plane];
        const auto* input = frame.planes[plane].data();
        for (std::uint32_t row = 0U; row < rows; ++row) {
            const auto source_row = frame.plane_strides[plane] >= 0
                                        ? row
                                        : rows - 1U - row;
            std::memcpy(output + static_cast<std::size_t>(row) * row_bytes,
                        input + static_cast<std::size_t>(source_row) * stride,
                        row_bytes);
        }
    }
}

PackedVideoFrame copy_video_frame(const VideoFrameView& frame) {
    const auto layout =
        packed_video_frame_layout(frame.width, frame.height, frame.pixel_format);
    PackedVideoFrame result{
        .sequence = frame.sequence,
        .presentation_timestamp_ns = frame.presentation_timestamp_ns,
        .duration_ns = frame.duration_ns,
        .produced_monotonic_ns = frame.produced_monotonic_ns,
        .discontinuity = frame.discontinuity,
        .width = frame.width,
        .height = frame.height,
        .pixel_format = frame.pixel_format,
        .plane_strides = layout.plane_strides,
        .plane_offsets = layout.plane_offsets,
        .bytes = std::vector<std::uint8_t>(
            static_cast<std::size_t>(layout.payload_size)),
    };
    copy_video_frame_pixels(frame, result.bytes);
    return result;
}

} // namespace solin::media_engine
