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

void test_invalid_format_does_not_poison_other_formats_or_cameras() {
    auto mixed = provider_device("camera://mixed", "Mixed formats");
    auto invalid = mixed.formats.front();
    invalid.fps_denominator = 0U;
    mixed.formats.push_back(invalid);
    auto unsupported = provider_device("camera://unsupported", "Unsupported formats");
    unsupported.formats.front().fps_numerator = 61U;
    const auto devices = solin::media_engine::reconcile_local_camera_devices(
        {.supported = true},
        {std::move(mixed), std::move(unsupported),
         provider_device("camera://healthy", "Healthy camera")});

    expect(devices.size() == 3U, "invalid formats never hide camera identities");
    expect(devices[1].formats.size() == 1U,
           "one bad format does not discard a camera's valid formats");
    expect(devices[1].rejected_format_count == 1U &&
               devices[1].format_rejections.front().error_code == "invalid_frame_rate" &&
               devices[1].format_rejections.front().format_index == 1U,
           "format rejection diagnostics identify the reason and input position");
    expect(devices[2].formats.empty() &&
               devices[2].probe.status ==
                   solin::media_engine::LocalCameraProbeStatus::unverified,
           "camera with no usable format stays selectable as unverified");
    expect(devices[0].formats.size() == 1U,
           "unrelated camera remains ready");
}

void test_provider_formats_are_canonical_exact_and_deduplicated() {
    auto provider = provider_device("camera://exact", "Exact frame rate");
    auto exact = provider.formats.front();
    exact.fps_numerator = 10'000'000U;
    exact.fps_denominator = 333'333U;
    auto equivalent = exact;
    equivalent.fps_numerator *= 2U;
    equivalent.fps_denominator *= 2U;
    provider.formats = {exact, equivalent};
    const auto devices = solin::media_engine::reconcile_local_camera_devices(
        {.supported = true}, {std::move(provider)});
    expect(devices[0].formats.size() == 1U,
           "equivalent fractions produce a single canonical format");
    expect(devices[0].formats.front() == exact,
           "high-precision driver FPS is preserved without rounding");
}

void test_format_validation_bounds_diagnostics_and_rejects_invalid_inputs() {
    const auto valid = provider_device("camera://valid", "Valid").formats.front();
    std::vector<solin::media_engine::LocalVideoFormat> invalid(12U, valid);
    invalid[0].fps_numerator = 2'147'483'648U;
    invalid[1].fps_denominator = 2'147'483'648U;
    invalid[2].media_type = "video/unknown";
    invalid[3].pixel_format = "";
    invalid[4].pixel_format = "NV12\n";
    invalid[5].pixel_format = std::string(81U, 'x');
    invalid[6].width = 0U;
    invalid[7].height = 3'841U;
    invalid[8].width = 3'840U;
    invalid[8].height = 3'840U;
    invalid[9].fps_numerator = 10'000'000U;
    invalid[9].fps_denominator = 166'666U;
    invalid[10].fps_numerator = 0U;
    invalid[11].fps_denominator = 0U;
    auto provider = provider_device("camera://invalid", "Invalid");
    provider.formats = invalid;
    const auto devices = solin::media_engine::reconcile_local_camera_devices(
        {.supported = true}, {std::move(provider)});
    expect(devices[0].formats.empty(), "invalid native formats never reach the wire");
    expect(devices[0].rejected_format_count == invalid.size() &&
               devices[0].format_rejections.size() == 8U,
           "format diagnostics count all rejections but retain bounded samples");
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
    test_invalid_format_does_not_poison_other_formats_or_cameras();
    test_provider_formats_are_canonical_exact_and_deduplicated();
    test_format_validation_bounds_diagnostics_and_rejects_invalid_inputs();
    test_platform_inventory_is_bounded_and_non_activating();
    return failures == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
