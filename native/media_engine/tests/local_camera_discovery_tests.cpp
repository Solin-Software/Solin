#include "local_camera_inventory.hpp"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <string>
#include <string_view>
#include <vector>

namespace {

int failures = 0;

void expect(const bool condition, const std::string& message) {
    if (!condition) {
        std::cerr << "FAIL: " << message << '\n';
        ++failures;
    }
}

[[nodiscard]] solin::media_engine::LocalCameraDevice provider_device(
    std::string device_id, std::string display_name) {
    return {
        .device_id = std::move(device_id),
        .display_name = std::move(display_name),
        .formats = {{
            .media_type = "video/x-raw",
            .pixel_format = "NV12",
            .width = 1'920U,
            .height = 1'080U,
            .fps_numerator = 30U,
            .fps_denominator = 1U,
        }},
    };
}

void test_inventory_only_device_remains_selectable_for_direct_capture() {
    const solin::media_engine::LocalCameraInventorySnapshot inventory{
        .supported = true,
        .devices = {{
            .device_id = R"(\\?\usb#vid_046d&pid_082d#c920)",
            .display_name = "HD Pro Webcam C920",
        }},
    };

    const auto devices = solin::media_engine::reconcile_local_camera_devices(
        inventory, {});

    expect(devices.size() == 1U, "inventory-only camera is retained");
    expect(devices[0].probe.status ==
               solin::media_engine::LocalCameraProbeStatus::unverified,
           "inventory-only camera is explicitly unverified");
    expect(devices[0].probe.error_code == "capture_provider_not_reported",
           "inventory-only camera has an actionable stable diagnostic");
    expect(devices[0].formats.empty(),
           "inventory-only camera negotiates its format during direct capture");
}

void test_provider_result_wins_case_insensitive_inventory_match() {
    const solin::media_engine::LocalCameraInventorySnapshot inventory{
        .supported = true,
        .devices = {{
            .device_id = R"(\\?\USB#VID_046D&PID_082D#C920)",
            .display_name = "Inventory name",
        }},
    };
    auto provider = provider_device(
        R"(\\?\usb#vid_046d&pid_082d#c920)", "Provider name");
    const auto provider_id = provider.device_id;

    const auto devices = solin::media_engine::reconcile_local_camera_devices(
        inventory, {std::move(provider)});

    expect(devices.size() == 1U, "case-only device ids are deduplicated");
    expect(devices[0].device_id == provider_id,
           "capture provider device id is preserved for mfvideosrc");
    expect(devices[0].display_name == "Provider name",
           "verified provider metadata takes precedence");
    expect(devices[0].probe.status ==
               solin::media_engine::LocalCameraProbeStatus::ready,
           "provider device is ready");
}

void test_inventory_failure_does_not_hide_provider_devices() {
    const solin::media_engine::LocalCameraInventorySnapshot inventory{
        .supported = true,
        .error_code = "camera_inventory_enumeration_failed",
        .native_error_code = "0x80004005",
    };

    const auto devices = solin::media_engine::reconcile_local_camera_devices(
        inventory, {provider_device("camera://provider", "Provider camera")});

    expect(devices.size() == 1U, "provider device survives inventory failure");
    expect(devices[0].probe.status ==
               solin::media_engine::LocalCameraProbeStatus::ready,
           "provider evidence remains authoritative");
}

void test_provider_device_without_bounded_formats_remains_unverified() {
    auto provider = provider_device("camera://empty-caps", "Empty caps camera");
    provider.formats.clear();

    const auto devices = solin::media_engine::reconcile_local_camera_devices(
        {.supported = true}, {std::move(provider)});

    expect(devices.size() == 1U, "provider camera with empty caps is retained");
    expect(devices[0].probe.status ==
               solin::media_engine::LocalCameraProbeStatus::unverified,
           "empty provider caps are not reported as verified");
    expect(devices[0].probe.error_code == "capture_formats_unavailable",
           "empty provider caps have a stable format diagnostic");
}

void test_reconciliation_deduplicates_and_bounds_devices() {
    std::vector<solin::media_engine::LocalCameraDevice> provider_devices;
    provider_devices.reserve(66U);
    for (std::size_t index = 0U; index < 65U; ++index) {
        provider_devices.push_back(provider_device(
            "camera://" + std::to_string(index),
            "Camera " + std::to_string(index)));
    }
    provider_devices.push_back(provider_device("CAMERA://0", "Duplicate"));
    const solin::media_engine::LocalCameraInventorySnapshot inventory{
        .supported = true,
        .devices = {{
            .device_id = "camera://inventory-only",
            .display_name = "Inventory camera",
        }},
    };

    const auto devices = solin::media_engine::reconcile_local_camera_devices(
        inventory, std::move(provider_devices));

    expect(devices.size() == 64U, "camera list is bounded at the protocol limit");
    expect(std::ranges::any_of(
               devices, [](const solin::media_engine::LocalCameraDevice& device) {
                   return device.device_id == "camera://inventory-only";
               }),
           "authoritative inventory devices take priority at the protocol limit");
}

void test_platform_inventory_is_bounded_and_non_activating() {
    const auto inventory =
        solin::media_engine::platform_local_camera_inventory();
#ifdef _WIN32
    expect(inventory.supported, "Media Foundation inventory is supported on Windows");
#else
    expect(!inventory.supported, "platform inventory is disabled outside Windows");
#endif
    expect(inventory.devices.size() <= 64U,
           "platform inventory respects the control-protocol device budget");
}

} // namespace

void benchmark_platform_inventory() {
    constexpr std::size_t iterations = 100U;
    std::vector<double> durations_ms;
    durations_ms.reserve(iterations);
    for (std::size_t index = 0U; index < iterations; ++index) {
        const auto started = std::chrono::steady_clock::now();
        static_cast<void>(
            solin::media_engine::platform_local_camera_inventory());
        const auto elapsed = std::chrono::steady_clock::now() - started;
        durations_ms.push_back(
            std::chrono::duration<double, std::milli>{elapsed}.count());
    }
    std::ranges::sort(durations_ms);
    const auto percentile = [&durations_ms](const double value) {
        const auto rank = static_cast<std::size_t>(
            value * static_cast<double>(durations_ms.size() - 1U));
        return durations_ms[rank];
    };
    std::cout << "camera_inventory_benchmark iterations=" << iterations
              << " p50_ms=" << percentile(0.50)
              << " p95_ms=" << percentile(0.95) << '\n';
}

int main(const int argument_count, const char* const* arguments) {
    if (argument_count == 2 &&
        std::string_view{arguments[1]} == "--benchmark") {
        benchmark_platform_inventory();
        return EXIT_SUCCESS;
    }
    test_inventory_only_device_remains_selectable_for_direct_capture();
    test_provider_result_wins_case_insensitive_inventory_match();
    test_inventory_failure_does_not_hide_provider_devices();
    test_provider_device_without_bounded_formats_remains_unverified();
    test_reconciliation_deduplicates_and_bounds_devices();
    test_platform_inventory_is_bounded_and_non_activating();
    return failures == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
