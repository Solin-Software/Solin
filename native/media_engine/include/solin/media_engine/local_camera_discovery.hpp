#pragma once

#include <cstdint>
#include <limits>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace solin::media_engine {

// GStreamer exact fractions use signed 32-bit numerator and denominator values.
inline constexpr std::uint32_t kMaximumCameraFpsComponent =
    static_cast<std::uint32_t>((std::numeric_limits<std::int32_t>::max)());

struct LocalVideoFormat {
    std::string media_type{};
    std::string pixel_format{};
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    std::uint32_t fps_numerator{0U};
    std::uint32_t fps_denominator{1U};

    bool operator==(const LocalVideoFormat&) const = default;
};

enum class LocalCameraProbeStatus {
    ready,
    unverified,
};

[[nodiscard]] constexpr std::string_view local_camera_probe_status_text(
    const LocalCameraProbeStatus status) noexcept {
    switch (status) {
    case LocalCameraProbeStatus::ready:
        return "ready";
    case LocalCameraProbeStatus::unverified:
        return "unverified";
    }
    return "unverified";
}

struct LocalCameraProbe {
    LocalCameraProbeStatus status{LocalCameraProbeStatus::ready};
    std::string backend{"media_foundation"};
    std::string failure_stage{};
    std::string error_code{};
    std::string native_error_code{};

    bool operator==(const LocalCameraProbe&) const = default;
};

// Internal diagnostics are logged locally, never added to the control payload.
struct LocalCameraFormatRejection {
    std::uint32_t format_index{0U};
    std::string error_code{};
    LocalVideoFormat format{};

    bool operator==(const LocalCameraFormatRejection&) const = default;
};

struct LocalCameraDevice {
    std::string device_id{};
    std::string display_name{};
    bool software_device{false};
    std::vector<LocalVideoFormat> formats{};
    LocalCameraProbe probe{};
    std::uint32_t rejected_format_count{0U};
    std::vector<LocalCameraFormatRejection> format_rejections{};

    bool operator==(const LocalCameraDevice&) const = default;
};

struct LocalCameraSnapshot {
    bool supported{false};
    bool ready{true};
    std::uint64_t generation{0U};
    std::vector<LocalCameraDevice> devices{};
    std::string error_code{"media_runtime_unavailable"};
};

[[nodiscard]] std::optional<LocalVideoFormat>
preferred_automatic_camera_format(const LocalCameraDevice& device);

} // namespace solin::media_engine
