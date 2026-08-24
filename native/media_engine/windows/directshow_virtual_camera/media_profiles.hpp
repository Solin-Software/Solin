#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace solin::media_engine::windows_virtual_camera {

enum class DirectShowPixelFormat : std::uint8_t {
    nv12,
    yuy2,
};

struct DirectShowMediaProfile final {
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    DirectShowPixelFormat pixel_format{DirectShowPixelFormat::nv12};
    std::uint32_t fps_numerator{30U};
    std::uint32_t fps_denominator{1U};

    [[nodiscard]] std::size_t sample_size() const noexcept;
    bool operator==(const DirectShowMediaProfile&) const = default;
};

inline constexpr std::array<DirectShowMediaProfile, 8U> kMediaProfiles{{
    {1920U, 1080U, DirectShowPixelFormat::nv12, 30U, 1U},
    {1280U, 720U, DirectShowPixelFormat::nv12, 30U, 1U},
    {640U, 360U, DirectShowPixelFormat::nv12, 30U, 1U},
    {640U, 480U, DirectShowPixelFormat::nv12, 30U, 1U},
    {1920U, 1080U, DirectShowPixelFormat::yuy2, 30U, 1U},
    {1280U, 720U, DirectShowPixelFormat::yuy2, 30U, 1U},
    {640U, 360U, DirectShowPixelFormat::yuy2, 30U, 1U},
    {640U, 480U, DirectShowPixelFormat::yuy2, 30U, 1U},
}};

[[nodiscard]] const DirectShowMediaProfile* find_media_profile(
    std::uint32_t width, std::uint32_t height,
    DirectShowPixelFormat pixel_format, std::uint32_t fps_numerator,
    std::uint32_t fps_denominator) noexcept;

// Rational 30 fps timeline in DirectShow's 100 ns units. The quotient/remainder
// form preserves alternating 333333/333334 durations without multiplication
// overflow or accumulated drift.
[[nodiscard]] std::int64_t directshow_frame_time_100ns(
    std::uint64_t frame_index) noexcept;

} // namespace solin::media_engine::windows_virtual_camera
