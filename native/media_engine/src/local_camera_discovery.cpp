#include "local_camera_inventory.hpp"

#include "solin/media_engine/virtual_camera_identity.hpp"

#include <algorithm>
#include <cstddef>
#include <string>
#include <tuple>
#include <utility>

namespace solin::media_engine {
namespace {

constexpr std::size_t kMaximumLocalCameraDevices = 64U;

void mark_provider_result(LocalCameraDevice& device) {
    if (device.formats.empty()) {
        device.probe = {
            .status = LocalCameraProbeStatus::unverified,
            .backend = "media_foundation",
            .failure_stage = "format_probe",
            .error_code = "capture_formats_unavailable",
        };
    } else {
        device.probe = {
            .status = LocalCameraProbeStatus::ready,
            .backend = "media_foundation",
        };
    }
}

[[nodiscard]] LocalCameraDevice unverified_device(
    const LocalCameraInventoryDevice& inventory_device) {
    return {
        .device_id = inventory_device.device_id,
        .display_name = inventory_device.display_name,
        .software_device = inventory_device.software_device,
        .formats = {},
        .probe = {
            .status = LocalCameraProbeStatus::unverified,
            .backend = "media_foundation",
            .failure_stage = "capture_provider",
            .error_code = "capture_provider_not_reported",
        },
    };
}

} // namespace

bool same_local_camera_device_id(const std::string_view left,
                                 const std::string_view right) noexcept {
    return left.size() == right.size() &&
           std::ranges::equal(left, right, [](const char left_character,
                                              const char right_character) {
               return ascii_lower(left_character) == ascii_lower(right_character);
           });
}

std::vector<LocalCameraDevice> reconcile_local_camera_devices(
    const LocalCameraInventorySnapshot& inventory,
    std::vector<LocalCameraDevice> provider_devices) {
    std::vector<LocalCameraDevice> unique_provider_devices;
    unique_provider_devices.reserve(provider_devices.size());
    for (auto& device : provider_devices) {
        if (std::ranges::any_of(
                unique_provider_devices,
                [&device](const LocalCameraDevice& candidate) {
                    return same_local_camera_device_id(candidate.device_id,
                                                       device.device_id);
                })) {
            continue;
        }
        unique_provider_devices.push_back(std::move(device));
    }

    std::vector<LocalCameraDevice> devices;
    devices.reserve((std::min)(inventory.devices.size() +
                                   unique_provider_devices.size(),
                               kMaximumLocalCameraDevices));
    std::vector<bool> consumed_provider_devices(unique_provider_devices.size(),
                                                false);
    if (inventory.supported && inventory.error_code.empty()) {
        for (const auto& inventory_device : inventory.devices) {
            if (devices.size() >= kMaximumLocalCameraDevices) {
                break;
            }
            if (std::ranges::any_of(
                    devices, [&inventory_device](const LocalCameraDevice& device) {
                    return same_local_camera_device_id(device.device_id,
                                                       inventory_device.device_id);
                })) {
                continue;
            }
            const auto provider = std::ranges::find_if(
                unique_provider_devices,
                [&inventory_device](const LocalCameraDevice& device) {
                    return same_local_camera_device_id(device.device_id,
                                                       inventory_device.device_id);
                });
            if (provider == unique_provider_devices.end()) {
                devices.push_back(unverified_device(inventory_device));
                continue;
            }
            const auto provider_index = static_cast<std::size_t>(
                std::distance(unique_provider_devices.begin(), provider));
            consumed_provider_devices[provider_index] = true;
            mark_provider_result(*provider);
            devices.push_back(std::move(*provider));
        }
    }
    for (std::size_t index = 0U;
         index < unique_provider_devices.size() &&
         devices.size() < kMaximumLocalCameraDevices;
         ++index) {
        if (consumed_provider_devices[index]) {
            continue;
        }
        auto& device = unique_provider_devices[index];
        mark_provider_result(device);
        devices.push_back(std::move(device));
    }
    std::ranges::sort(
        devices,
        [](const LocalCameraDevice& left, const LocalCameraDevice& right) {
            return std::tie(left.display_name, left.device_id) <
                   std::tie(right.display_name, right.device_id);
        });
    return devices;
}

} // namespace solin::media_engine
