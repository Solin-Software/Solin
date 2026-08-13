#include "media_profiles.hpp"

#include <limits>

namespace solin::media_engine::windows_virtual_camera {

std::size_t DirectShowMediaProfile::sample_size() const noexcept {
    const auto pixels = static_cast<std::uint64_t>(width) * height;
    const auto bytes = pixel_format == DirectShowPixelFormat::nv12
                           ? pixels * 3U / 2U
                           : pixels * 2U;
    return bytes <= (std::numeric_limits<std::size_t>::max)()
               ? static_cast<std::size_t>(bytes)
               : 0U;
}

const DirectShowMediaProfile* find_media_profile(
    const std::uint32_t width, const std::uint32_t height,
    const DirectShowPixelFormat pixel_format,
    const std::uint32_t fps_numerator,
    const std::uint32_t fps_denominator) noexcept {
    for (const auto& profile : kMediaProfiles) {
        if (profile.width == width && profile.height == height &&
            profile.pixel_format == pixel_format &&
            static_cast<std::uint64_t>(profile.fps_numerator) *
                    fps_denominator ==
                static_cast<std::uint64_t>(fps_numerator) *
                    profile.fps_denominator) {
            return &profile;
        }
    }
    return nullptr;
}

std::int64_t directshow_frame_time_100ns(
    const std::uint64_t frame_index) noexcept {
    constexpr std::uint64_t units_per_second = 10'000'000ULL;
    constexpr auto maximum =
        static_cast<std::uint64_t>((std::numeric_limits<std::int64_t>::max)());
    const auto seconds = frame_index / 30U;
    if (seconds > maximum / units_per_second) {
        return (std::numeric_limits<std::int64_t>::max)();
    }
    const auto whole = seconds * units_per_second;
    const auto fraction =
        (frame_index % 30U) * units_per_second / 30U;
    if (whole > maximum - fraction) {
        return (std::numeric_limits<std::int64_t>::max)();
    }
    return static_cast<std::int64_t>(whole + fraction);
}

} // namespace solin::media_engine::windows_virtual_camera
