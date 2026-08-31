#include "local_camera_inventory.hpp"

#include "solin/media_engine/virtual_camera_identity.hpp"

#include <algorithm>
#include <cstddef>
#include <numeric>
#include <string>
#include <tuple>
#include <utility>

namespace solin::media_engine {
namespace {

constexpr std::size_t kMaximumLocalCameraDevices = 64U;

void mark_provider_result(LocalCameraDevice& device) {
    std::vector<LocalVideoFormat> formats;
    formats.reserve((std::min)(device.formats.size(), std::size_t{256U}));
    for (std::size_t index = 0U; index < device.formats.size(); ++index) {
        auto format = std::move(device.formats[index]);
        const auto error = normalize_local_camera_format(format);
        if (!error.empty()) {
            record_local_camera_format_rejection(
                device, static_cast<std::uint32_t>(index), error, format);
        } else if (formats.size() < 256U &&
                   std::ranges::find(formats, format) == formats.end()) {
            formats.push_back(std::move(format));
        }
    }
    device.formats = std::move(formats);
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

std::string_view normalize_local_camera_format(LocalVideoFormat& format) {
    if (format.media_type != "video/x-raw" && format.media_type != "image/jpeg" &&
        format.media_type != "video/x-h264") {
        return "unsupported_media_type";
    }
    if (format.pixel_format.empty() || format.pixel_format.size() > 80U ||
        std::ranges::any_of(format.pixel_format, [](const unsigned char value) {
            return value < 32U || value == 127U;
        })) {
        return "invalid_pixel_format";
    }
    if (format.width == 0U || format.height == 0U ||
        format.width > 3'840U || format.height > 3'840U ||
        static_cast<std::uint64_t>(format.width) * format.height > 3'840ULL * 2'160ULL ||
        (std::min)(format.width, format.height) > 2'160U) {
        return "invalid_dimensions";
    }
    if (format.fps_numerator == 0U || format.fps_denominator == 0U ||
        format.fps_numerator > kMaximumCameraFpsComponent ||
        format.fps_denominator > kMaximumCameraFpsComponent ||
        static_cast<std::uint64_t>(format.fps_numerator) >
            60ULL * format.fps_denominator) {
        return "invalid_frame_rate";
    }
    const auto divisor = std::gcd(format.fps_numerator, format.fps_denominator);
    format.fps_numerator /= divisor;
    format.fps_denominator /= divisor;
    return {};
}

void record_local_camera_format_rejection(LocalCameraDevice& device,
                                          const std::uint32_t index,
                                          const std::string_view error_code,
                                          const LocalVideoFormat& format) {
    ++device.rejected_format_count;
    constexpr std::size_t maximum_samples = 8U;
    if (device.format_rejections.size() < maximum_samples) {
        device.format_rejections.push_back({index, std::string{error_code}, format});
    }
}

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
