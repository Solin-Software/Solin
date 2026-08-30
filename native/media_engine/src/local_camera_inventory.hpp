#pragma once

#include "solin/media_engine/local_camera_discovery.hpp"

#include <string>
#include <string_view>
#include <vector>

namespace solin::media_engine {

struct LocalCameraInventoryDevice {
    std::string device_id{};
    std::string display_name{};
    bool software_device{false};

    bool operator==(const LocalCameraInventoryDevice&) const = default;
};

struct LocalCameraInventorySnapshot {
    bool supported{false};
    std::vector<LocalCameraInventoryDevice> devices{};
    std::string error_code{};
    std::string native_error_code{};
};

[[nodiscard]] LocalCameraInventorySnapshot platform_local_camera_inventory() noexcept;

[[nodiscard]] bool same_local_camera_device_id(std::string_view left,
                                               std::string_view right) noexcept;

[[nodiscard]] std::vector<LocalCameraDevice> reconcile_local_camera_devices(
    const LocalCameraInventorySnapshot& inventory,
    std::vector<LocalCameraDevice> provider_devices);

} // namespace solin::media_engine
