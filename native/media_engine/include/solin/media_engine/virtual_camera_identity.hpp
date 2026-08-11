#pragma once

#include <string_view>

namespace solin::media_engine {

inline constexpr std::string_view kSolinVirtualCameraId{"solin-main-camera"};
inline constexpr std::string_view kSolinVirtualCameraFriendlyName{
    "Solin Virtual Camera"};

[[nodiscard]] constexpr bool
is_windows_software_camera_device(const std::string_view device_id) noexcept {
    return device_id.starts_with("@device:sw:") ||
           device_id.starts_with(R"(\\?\swd#vcamdevapi#)");
}

[[nodiscard]] constexpr bool is_solin_virtual_camera_device(
    const std::string_view device_id,
    const std::string_view display_name) noexcept {
    if (!is_windows_software_camera_device(device_id) ||
        !display_name.starts_with(kSolinVirtualCameraFriendlyName)) {
        return false;
    }
    const auto suffix = display_name.substr(kSolinVirtualCameraFriendlyName.size());
    return suffix.empty() ||
           (suffix.size() >= 3U && suffix.starts_with(" (") &&
            suffix.ends_with(')'));
}

} // namespace solin::media_engine
