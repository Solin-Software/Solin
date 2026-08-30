#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace solin::media_engine {

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

struct LocalCameraDevice {
    std::string device_id{};
    std::string display_name{};
    bool software_device{false};
    std::vector<LocalVideoFormat> formats{};
    LocalCameraProbe probe{};

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
