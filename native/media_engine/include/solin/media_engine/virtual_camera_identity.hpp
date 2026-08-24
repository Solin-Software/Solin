#pragma once

#include <cstddef>
#include <string_view>

namespace solin::media_engine {

inline constexpr std::string_view kSolinVirtualCameraId{"solin-main-camera"};
inline constexpr std::string_view kSolinVirtualCameraFriendlyName{
    "Solin Virtual Camera"};
inline constexpr std::string_view kSolinDirectShowVirtualCameraClassId{
    "{08afa2e5-0293-4e56-9fe1-2a79dae8e28f}"};

[[nodiscard]] constexpr bool
is_windows_software_camera_device(const std::string_view device_id) noexcept {
    return device_id.starts_with("@device:sw:") ||
           device_id.starts_with(R"(\\?\swd#)");
}

[[nodiscard]] constexpr char ascii_lower(const char value) noexcept {
    return value >= 'A' && value <= 'Z' ? static_cast<char>(value + ('a' - 'A'))
                                        : value;
}

[[nodiscard]] constexpr bool contains_ascii_case_insensitive(
    const std::string_view value, const std::string_view expected) noexcept {
    if (expected.empty() || expected.size() > value.size()) {
        return false;
    }
    for (std::size_t offset = 0U; offset <= value.size() - expected.size();
         ++offset) {
        bool matches = true;
        for (std::size_t index = 0U; index < expected.size(); ++index) {
            if (ascii_lower(value[offset + index]) !=
                ascii_lower(expected[index])) {
                matches = false;
                break;
            }
        }
        if (matches) {
            return true;
        }
    }
    return false;
}

[[nodiscard]] constexpr bool is_solin_virtual_camera_device(
    const std::string_view device_id) noexcept {
    return contains_ascii_case_insensitive(
        device_id, kSolinDirectShowVirtualCameraClassId);
}

} // namespace solin::media_engine
