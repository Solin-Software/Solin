#include "solin/media_engine/media_runtime.hpp"

#include "gstreamer_source_runtime.hpp"
#include "gstreamer_scene_renderer.hpp"
#include "solin/media_engine/virtual_camera_identity.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <exception>
#include <memory>
#include <mutex>
#include <numeric>
#include <string>
#include <thread>
#include <tuple>
#include <utility>
#include <vector>

#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
#include <gst/gst.h>
#endif

namespace solin::media_engine {
namespace {

#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
constexpr std::size_t kMaximumDevices = 64U;
constexpr std::size_t kMaximumFormatsPerDevice = 256U;
constexpr std::size_t kMaximumDeviceIdBytes = 1024U;
constexpr std::size_t kMaximumDisplayNameBytes = 512U;
constexpr std::int64_t kMaximumSourcePixels = 3'840LL * 2'160LL;
constexpr auto kInitialDeviceMonitorRetry = std::chrono::milliseconds{250};
constexpr auto kMaximumDeviceMonitorRetry = std::chrono::milliseconds{8'000};
constexpr auto kDeviceMonitorStartupTimeout = std::chrono::seconds{5};
constexpr auto kStableDeviceMonitorRun = std::chrono::seconds{10};

[[nodiscard]] bool has_element_factory(const char* name) {
    auto* factory = gst_element_factory_find(name);
    if (factory == nullptr) {
        return false;
    }
    gst_object_unref(factory);
    return true;
}

[[nodiscard]] std::string runtime_version() {
    auto* raw_version = gst_version_string();
    if (raw_version == nullptr) {
        return {};
    }
    std::string version{raw_version};
    g_free(raw_version);
    return version;
}

[[nodiscard]] bool is_bounded_text(const std::string& value, const std::size_t maximum) {
    return !value.empty() && value.size() <= maximum &&
           std::ranges::none_of(value, [](const unsigned char character) {
               return character < 32U || character == 127U;
           }) &&
           g_utf8_validate(value.data(), static_cast<gssize>(value.size()), nullptr) != FALSE;
}

[[nodiscard]] std::string bounded_utf8(const char* value, const std::size_t maximum) {
    if (value == nullptr ||
        g_utf8_validate(value, static_cast<gssize>(std::char_traits<char>::length(value)), nullptr) ==
            FALSE) {
        return {};
    }
    std::string result{value};
    if (result.size() <= maximum) {
        return result;
    }
    result.resize(maximum);
    while (!result.empty() &&
           g_utf8_validate(result.data(), static_cast<gssize>(result.size()), nullptr) == FALSE) {
        result.pop_back();
    }
    return result;
}

[[nodiscard]] std::string bounded_display_name(const char* value) {
    auto result = bounded_utf8(value, kMaximumDisplayNameBytes);
    std::ranges::replace_if(
        result,
        [](const unsigned char character) { return character < 32U || character == 127U; },
        ' ');
    return result;
}

[[nodiscard]] std::string pixel_format_for(const GstStructure* structure) {
    const auto* media_type = gst_structure_get_name(structure);
    if (std::string_view{media_type} == "video/x-raw") {
        return bounded_utf8(gst_structure_get_string(structure, "format"), 80U);
    }
    if (std::string_view{media_type} == "image/jpeg") {
        return "JPEG";
    }
    if (std::string_view{media_type} == "video/x-h264") {
        return "H264";
    }
    return {};
}

void release_caps(GstCaps* caps) noexcept { gst_caps_unref(caps); }

void release_structure(GstStructure* structure) noexcept { gst_structure_free(structure); }

[[nodiscard]] std::vector<LocalVideoFormat> formats_for(GstDevice* device) {
    std::vector<LocalVideoFormat> formats;
    const std::unique_ptr<GstCaps, decltype(&release_caps)> caps{
        gst_device_get_caps(device),
        &release_caps,
    };
    if (caps == nullptr) {
        return formats;
    }
    const auto structure_count = gst_caps_get_size(caps.get());
    for (guint index = 0U;
         index < structure_count && formats.size() < kMaximumFormatsPerDevice;
         ++index) {
        const auto* structure = gst_caps_get_structure(caps.get(), index);
        gint width = 0;
        gint height = 0;
        gint fps_numerator = 0;
        gint fps_denominator = 0;
        const auto pixel_format = pixel_format_for(structure);
        const auto* raw_media_type = gst_structure_get_name(structure);
        const std::string media_type = bounded_utf8(raw_media_type, 80U);
        if (pixel_format.empty() || media_type.empty() ||
            gst_structure_get_int(structure, "width", &width) == FALSE ||
            gst_structure_get_int(structure, "height", &height) == FALSE ||
            gst_structure_get_fraction(structure, "framerate", &fps_numerator,
                                       &fps_denominator) == FALSE ||
            width <= 0 || width > 3'840 || height <= 0 || height > 3'840 ||
            static_cast<std::int64_t>(width) * height > kMaximumSourcePixels ||
            (std::min)(width, height) > 2'160 ||
            fps_numerator <= 0 || fps_denominator <= 0 ||
            static_cast<std::int64_t>(fps_numerator) >
                60LL * static_cast<std::int64_t>(fps_denominator)) {
            continue;
        }
        const auto divisor = std::gcd(fps_numerator, fps_denominator);
        formats.push_back({
            .media_type = media_type,
            .pixel_format = pixel_format,
            .width = static_cast<std::uint32_t>(width),
            .height = static_cast<std::uint32_t>(height),
            .fps_numerator = static_cast<std::uint32_t>(fps_numerator / divisor),
            .fps_denominator = static_cast<std::uint32_t>(fps_denominator / divisor),
        });
    }
    std::ranges::sort(formats, [](const LocalVideoFormat& left, const LocalVideoFormat& right) {
        return std::tie(left.width, left.height, left.fps_numerator, left.fps_denominator,
                        left.media_type, left.pixel_format) >
               std::tie(right.width, right.height, right.fps_numerator, right.fps_denominator,
                        right.media_type, right.pixel_format);
    });
    formats.erase(std::ranges::unique(formats).begin(), formats.end());
    return formats;
}

void release_device_list(GList* entries) noexcept {
    for (auto* entry = entries; entry != nullptr; entry = entry->next) {
        gst_object_unref(entry->data);
    }
    g_list_free(entries);
}

[[nodiscard]] std::vector<LocalCameraDevice> enumerate_media_foundation_devices(
    GstDeviceMonitor* monitor) {
    std::vector<LocalCameraDevice> devices;
    const std::unique_ptr<GList, decltype(&release_device_list)> entries{
        gst_device_monitor_get_devices(monitor),
        &release_device_list,
    };
    for (auto* entry = entries.get(); entry != nullptr && devices.size() < kMaximumDevices;
         entry = entry->next) {
        auto* device = GST_DEVICE(entry->data);
        const std::unique_ptr<GstStructure, decltype(&release_structure)> properties{
            gst_device_get_properties(device),
            &release_structure,
        };
        const auto* raw_api =
            properties == nullptr ? nullptr
                                  : gst_structure_get_string(properties.get(), "device.api");
        const auto* raw_id =
            properties == nullptr ? nullptr
                                  : gst_structure_get_string(properties.get(), "device.path");
        const std::string api = bounded_utf8(raw_api, 80U);
        const std::string device_id = bounded_utf8(raw_id, kMaximumDeviceIdBytes);
        if (api == "mediafoundation" &&
            is_bounded_text(device_id, kMaximumDeviceIdBytes)) {
            const std::unique_ptr<gchar, decltype(&g_free)> raw_display_name{
                gst_device_get_display_name(device),
                &g_free,
            };
            auto display_name = bounded_display_name(raw_display_name.get());
            const auto software_device =
                is_windows_software_camera_device(device_id);
            if (is_solin_virtual_camera_device(device_id)) {
                // Feeding Solin's own output back into its compositor creates a
                // recursive graph and can exhaust the Windows sample allocator.
                // It is an output device, never a scene input.
                continue;
            }
            devices.push_back({
                .device_id = device_id,
                .display_name = std::move(display_name),
                .software_device = software_device,
                .formats = formats_for(device),
            });
        }
    }
    std::ranges::sort(devices, [](const LocalCameraDevice& left, const LocalCameraDevice& right) {
        return std::tie(left.display_name, left.device_id) <
               std::tie(right.display_name, right.device_id);
    });
    std::vector<LocalCameraDevice> unique_devices;
    unique_devices.reserve(devices.size());
    for (auto& device : devices) {
        const bool duplicate =
            std::ranges::any_of(unique_devices, [&device](const LocalCameraDevice& candidate) {
                return candidate.device_id == device.device_id;
            });
        if (!duplicate) {
            unique_devices.push_back(std::move(device));
        }
    }
    return unique_devices;
}

[[nodiscard]] std::vector<AudioDevice> enumerate_wasapi_devices(GstDeviceMonitor* monitor) {
    std::vector<AudioDevice> devices;
    std::optional<std::string> default_input_id;
    std::optional<std::string> default_output_id;
    const std::unique_ptr<GList, decltype(&release_device_list)> entries{
        gst_device_monitor_get_devices(monitor),
        &release_device_list,
    };
    for (auto* entry = entries.get(); entry != nullptr; entry = entry->next) {
        auto* device = GST_DEVICE(entry->data);
        const std::unique_ptr<GstStructure, decltype(&release_structure)> properties{
            gst_device_get_properties(device), &release_structure};
        if (properties == nullptr) {
            continue;
        }
        const auto api = bounded_utf8(
            gst_structure_get_string(properties.get(), "device.api"), 80U);
        if (api != "wasapi2") {
            continue;
        }
        gboolean is_default = FALSE;
        static_cast<void>(gst_structure_get_boolean(properties.get(), "device.default",
                                                     &is_default));
        gboolean loopback = FALSE;
        static_cast<void>(gst_structure_get_boolean(
            properties.get(), "wasapi2.device.loopback", &loopback));
        if (is_default != FALSE) {
            const auto actual_id = bounded_utf8(
                gst_structure_get_string(properties.get(), "device.actual-id"),
                kMaximumDeviceIdBytes);
            if (is_bounded_text(actual_id, kMaximumDeviceIdBytes)) {
                if (loopback != FALSE || gst_device_has_classes(device, "Audio/Sink")) {
                    default_output_id = actual_id;
                } else {
                    default_input_id = actual_id;
                }
            }
            continue;
        }
        const auto device_id = bounded_utf8(
            gst_structure_get_string(properties.get(), "device.id"), kMaximumDeviceIdBytes);
        if (!is_bounded_text(device_id, kMaximumDeviceIdBytes)) {
            continue;
        }
        std::optional<AudioDeviceDirection> direction;
        if (gst_device_has_classes(device, "Audio/Sink")) {
            direction = AudioDeviceDirection::output;
        } else if (gst_device_has_classes(device, "Audio/Source") && loopback == FALSE) {
            direction = AudioDeviceDirection::input;
        }
        if (!direction.has_value()) {
            continue;
        }
        const std::unique_ptr<gchar, decltype(&g_free)> raw_display_name{
            gst_device_get_display_name(device), &g_free};
        auto display_name = bounded_display_name(raw_display_name.get());
        if (display_name.empty()) {
            display_name = device_id;
        }
        devices.push_back({
            .device_id = device_id,
            .display_name = std::move(display_name),
            .direction = direction.value(),
        });
    }
    for (auto& device : devices) {
        device.is_default =
            (device.direction == AudioDeviceDirection::input &&
             default_input_id == device.device_id) ||
            (device.direction == AudioDeviceDirection::output &&
             default_output_id == device.device_id);
    }
    std::ranges::sort(devices, [](const AudioDevice& left, const AudioDevice& right) {
        return std::tie(left.direction, left.display_name, left.device_id) <
               std::tie(right.direction, right.display_name, right.device_id);
    });
    const auto duplicates = std::ranges::unique(devices, {}, [](const AudioDevice& device) {
        return std::tie(device.direction, device.device_id);
    });
    devices.erase(duplicates.begin(), duplicates.end());
    if (devices.size() > kMaximumDevices) {
        devices.resize(kMaximumDevices);
    }
    return devices;
}
#endif

} // namespace

std::optional<LocalVideoFormat>
preferred_automatic_camera_format(const LocalCameraDevice& device) {
    if (device.formats.empty()) {
        return std::nullopt;
    }
    const auto score = [](const LocalVideoFormat& format) {
        const auto pixels = static_cast<std::uint64_t>(format.width) * format.height;
        const auto within_default_raster =
            format.width <= 1'920U && format.height <= 1'080U;
        const auto within_default_rate =
            format.fps_numerator <= 30U * format.fps_denominator;
        const auto media_rank = format.media_type == "video/x-raw"
                                    ? 2
                                    : format.media_type == "image/jpeg" ? 1 : 0;
        const auto pixel_rank = format.pixel_format == "NV12"
                                    ? 4
                                    : format.pixel_format == "YUY2"
                                          ? 3
                                          : format.pixel_format == "I420" ? 2 : 1;
        return std::tuple{within_default_raster, pixels, within_default_rate,
                          media_rank, pixel_rank};
    };
    const auto selected = std::ranges::max_element(
        device.formats, [&score](const LocalVideoFormat& left,
                                 const LocalVideoFormat& right) {
            const auto left_score = score(left);
            const auto right_score = score(right);
            if (left_score != right_score) {
                return left_score < right_score;
            }
            return static_cast<std::uint64_t>(left.fps_numerator) *
                       right.fps_denominator <
                   static_cast<std::uint64_t>(right.fps_numerator) *
                       left.fps_denominator;
        });
    return selected == device.formats.end()
               ? std::nullopt
               : std::optional<LocalVideoFormat>{*selected};
}

class MediaRuntime::DeviceMonitor final {
  public:
    DeviceMonitor() = default;
    ~DeviceMonitor() { stop(); }

    DeviceMonitor(const DeviceMonitor&) = delete;
    DeviceMonitor& operator=(const DeviceMonitor&) = delete;

    void start() {
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
        {
            std::scoped_lock lock{mutex_};
            snapshot_ = {
                .supported = true,
                .ready = false,
                .generation = snapshot_.generation,
                .devices = {},
                .error_code = {},
            };
        }
        stop_requested_.store(false);
        try {
            worker_ = std::thread([this] { run(); });
        } catch (const std::exception&) {
            publish_failure(false, "device_monitor_start_failed");
        }
#endif
    }

    void stop() {
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
        stop_requested_.store(true);
        retry_wakeup_.notify_all();
        if (worker_.joinable()) {
            worker_.join();
        }
#endif
    }

    void mark_unavailable(std::string error_code) {
        std::scoped_lock lock{mutex_};
        snapshot_.supported = false;
        snapshot_.ready = true;
        snapshot_.error_code = std::move(error_code);
        ++snapshot_.generation;
    }

    [[nodiscard]] LocalCameraSnapshot snapshot() const {
        std::scoped_lock lock{mutex_};
        return snapshot_;
    }

  private:
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
    void run() {
        auto retry_delay = kInitialDeviceMonitorRetry;
        const auto wait_for_next_attempt = [this, &retry_delay] {
            wait_before_retry(retry_delay);
            retry_delay =
                (std::min)(retry_delay * 2, kMaximumDeviceMonitorRetry);
        };
        while (!stop_requested_.load()) {
            auto* monitor = gst_device_monitor_new();
            if (monitor == nullptr) {
                publish_failure(false, "device_monitor_unavailable");
                wait_for_next_attempt();
                continue;
            }
            const auto filter_id =
                gst_device_monitor_add_filter(monitor, "Video/Source", nullptr);
            auto* bus = gst_device_monitor_get_bus(monitor);
            if (filter_id == 0U || bus == nullptr ||
                gst_device_monitor_start(monitor) == FALSE) {
                if (bus != nullptr) {
                    gst_object_unref(bus);
                }
                gst_object_unref(monitor);
                publish_failure(false, "device_provider_unavailable");
                wait_for_next_attempt();
                continue;
            }
            constexpr auto message_mask = static_cast<GstMessageType>(
                GST_MESSAGE_DEVICE_ADDED | GST_MESSAGE_DEVICE_REMOVED |
                GST_MESSAGE_DEVICE_MONITOR_STARTED | GST_MESSAGE_ERROR);
            bool monitor_ready = false;
            auto monitor_ready_since = std::chrono::steady_clock::time_point{};
            const auto startup_deadline =
                std::chrono::steady_clock::now() + kDeviceMonitorStartupTimeout;
            try {
                while (!stop_requested_.load()) {
                    auto* message =
                        gst_bus_timed_pop_filtered(bus, 100U * GST_MSECOND, message_mask);
                    if (message == nullptr) {
                        if (!monitor_ready &&
                            std::chrono::steady_clock::now() >= startup_deadline) {
                            publish_failure(false, "device_provider_start_timeout");
                            break;
                        }
                        continue;
                    }
                    const auto message_type = GST_MESSAGE_TYPE(message);
                    gboolean monitor_started = FALSE;
                    if (message_type == GST_MESSAGE_DEVICE_MONITOR_STARTED) {
                        gst_message_parse_device_monitor_started(message, &monitor_started);
                    }
                    gst_message_unref(message);
                    if (message_type == GST_MESSAGE_DEVICE_MONITOR_STARTED) {
                        if (monitor_started == FALSE) {
                            publish_failure(false, "device_provider_unavailable");
                            break;
                        }
                        monitor_ready = true;
                        monitor_ready_since = std::chrono::steady_clock::now();
                        publish_devices(enumerate_media_foundation_devices(monitor));
                    } else if (monitor_ready &&
                               (message_type == GST_MESSAGE_DEVICE_ADDED ||
                                message_type == GST_MESSAGE_DEVICE_REMOVED)) {
                        publish_devices(enumerate_media_foundation_devices(monitor));
                    } else if (message_type == GST_MESSAGE_ERROR) {
                        publish_failure(true, "device_monitor_failed");
                        break;
                    }
                }
            } catch (const std::exception&) {
                publish_failure(true, "device_monitor_failed");
            }
            gst_device_monitor_stop(monitor);
            gst_object_unref(bus);
            gst_object_unref(monitor);
            if (stop_requested_.load()) {
                break;
            }
            if (monitor_ready &&
                std::chrono::steady_clock::now() - monitor_ready_since >=
                    kStableDeviceMonitorRun) {
                retry_delay = kInitialDeviceMonitorRetry;
            }
            wait_for_next_attempt();
        }
    }

    void wait_before_retry(const std::chrono::milliseconds delay) {
        std::unique_lock lock{retry_mutex_};
        retry_wakeup_.wait_for(lock, delay,
                               [this] { return stop_requested_.load(); });
    }

    void publish_devices(std::vector<LocalCameraDevice> devices) {
        std::scoped_lock lock{mutex_};
        const bool changed = !snapshot_.ready || snapshot_.devices != devices ||
                             !snapshot_.error_code.empty() || !snapshot_.supported;
        snapshot_.supported = true;
        snapshot_.ready = true;
        snapshot_.devices = std::move(devices);
        snapshot_.error_code.clear();
        if (changed) {
            ++snapshot_.generation;
        }
    }

    void publish_failure(const bool supported, std::string error_code) {
        std::scoped_lock lock{mutex_};
        const bool changed = !snapshot_.ready || snapshot_.supported != supported ||
                             snapshot_.error_code != error_code ||
                             !snapshot_.devices.empty();
        snapshot_.supported = supported;
        snapshot_.ready = true;
        snapshot_.devices.clear();
        snapshot_.error_code = std::move(error_code);
        if (changed) {
            ++snapshot_.generation;
        }
    }

    std::atomic_bool stop_requested_{false};
    std::thread worker_{};
    std::mutex retry_mutex_{};
    std::condition_variable retry_wakeup_{};
#endif
    mutable std::mutex mutex_{};
    LocalCameraSnapshot snapshot_{};
};

class MediaRuntime::RuntimeSession final {
  public:
    RuntimeSession() = default;
};

class MediaRuntime::AudioDeviceMonitor final {
  public:
    AudioDeviceMonitor() = default;
    ~AudioDeviceMonitor() { stop(); }

    AudioDeviceMonitor(const AudioDeviceMonitor&) = delete;
    AudioDeviceMonitor& operator=(const AudioDeviceMonitor&) = delete;

    void start() {
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
        {
            std::scoped_lock lock{mutex_};
            snapshot_ = {.supported = true,
                         .ready = false,
                         .generation = snapshot_.generation,
                         .devices = {},
                         .error_code = {}};
        }
        stop_requested_.store(false);
        try {
            worker_ = std::thread([this] { run(); });
        } catch (const std::exception&) {
            publish_failure(false, "audio_device_monitor_start_failed");
        }
#endif
    }

    void stop() {
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
        stop_requested_.store(true);
        retry_wakeup_.notify_all();
        if (worker_.joinable()) {
            worker_.join();
        }
#endif
    }

    void mark_unavailable(std::string error_code) {
        std::scoped_lock lock{mutex_};
        snapshot_.supported = false;
        snapshot_.ready = true;
        snapshot_.error_code = std::move(error_code);
        ++snapshot_.generation;
    }

    [[nodiscard]] AudioDeviceSnapshot snapshot() const {
        std::scoped_lock lock{mutex_};
        return snapshot_;
    }

  private:
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
    void run() {
        auto retry_delay = kInitialDeviceMonitorRetry;
        while (!stop_requested_.load()) {
            auto* monitor = gst_device_monitor_new();
            if (monitor == nullptr) {
                publish_failure(false, "audio_device_monitor_unavailable");
                wait_before_retry(retry_delay);
                retry_delay = (std::min)(retry_delay * 2, kMaximumDeviceMonitorRetry);
                continue;
            }
            const auto source_filter =
                gst_device_monitor_add_filter(monitor, "Audio/Source", nullptr);
            const auto sink_filter =
                gst_device_monitor_add_filter(monitor, "Audio/Sink", nullptr);
            auto* bus = gst_device_monitor_get_bus(monitor);
            if ((source_filter == 0U && sink_filter == 0U) || bus == nullptr ||
                gst_device_monitor_start(monitor) == FALSE) {
                if (bus != nullptr) {
                    gst_object_unref(bus);
                }
                gst_object_unref(monitor);
                publish_failure(false, "audio_device_provider_unavailable");
                wait_before_retry(retry_delay);
                retry_delay = (std::min)(retry_delay * 2, kMaximumDeviceMonitorRetry);
                continue;
            }
            constexpr auto mask = static_cast<GstMessageType>(
                GST_MESSAGE_DEVICE_ADDED | GST_MESSAGE_DEVICE_REMOVED |
                GST_MESSAGE_DEVICE_CHANGED | GST_MESSAGE_DEVICE_MONITOR_STARTED |
                GST_MESSAGE_ERROR);
            const auto startup_deadline =
                std::chrono::steady_clock::now() + kDeviceMonitorStartupTimeout;
            bool ready = false;
            while (!stop_requested_.load()) {
                auto* message = gst_bus_timed_pop_filtered(bus, 100U * GST_MSECOND, mask);
                if (message == nullptr) {
                    if (!ready && std::chrono::steady_clock::now() >= startup_deadline) {
                        publish_failure(false, "audio_device_provider_start_timeout");
                        break;
                    }
                    continue;
                }
                const auto type = GST_MESSAGE_TYPE(message);
                gboolean started = FALSE;
                if (type == GST_MESSAGE_DEVICE_MONITOR_STARTED) {
                    gst_message_parse_device_monitor_started(message, &started);
                }
                gst_message_unref(message);
                if (type == GST_MESSAGE_DEVICE_MONITOR_STARTED && started != FALSE) {
                    ready = true;
                    retry_delay = kInitialDeviceMonitorRetry;
                    publish_devices(enumerate_wasapi_devices(monitor));
                } else if (ready && (type == GST_MESSAGE_DEVICE_ADDED ||
                                     type == GST_MESSAGE_DEVICE_REMOVED ||
                                     type == GST_MESSAGE_DEVICE_CHANGED)) {
                    publish_devices(enumerate_wasapi_devices(monitor));
                } else if (type == GST_MESSAGE_ERROR ||
                           (type == GST_MESSAGE_DEVICE_MONITOR_STARTED && started == FALSE)) {
                    publish_failure(true, "audio_device_monitor_failed");
                    break;
                }
            }
            gst_device_monitor_stop(monitor);
            gst_object_unref(bus);
            gst_object_unref(monitor);
            if (!stop_requested_.load()) {
                wait_before_retry(retry_delay);
                retry_delay = (std::min)(retry_delay * 2, kMaximumDeviceMonitorRetry);
            }
        }
    }

    void wait_before_retry(const std::chrono::milliseconds delay) {
        std::unique_lock lock{retry_mutex_};
        retry_wakeup_.wait_for(lock, delay, [this] { return stop_requested_.load(); });
    }

    void publish_devices(std::vector<AudioDevice> devices) {
        std::scoped_lock lock{mutex_};
        const bool changed = !snapshot_.ready || !snapshot_.supported ||
                             !snapshot_.error_code.empty() || snapshot_.devices != devices;
        snapshot_.supported = true;
        snapshot_.ready = true;
        snapshot_.devices = std::move(devices);
        snapshot_.error_code.clear();
        if (changed) {
            ++snapshot_.generation;
        }
    }

    void publish_failure(const bool supported, std::string error_code) {
        std::scoped_lock lock{mutex_};
        const bool changed = !snapshot_.ready || snapshot_.supported != supported ||
                             snapshot_.error_code != error_code || !snapshot_.devices.empty();
        snapshot_.supported = supported;
        snapshot_.ready = true;
        snapshot_.devices.clear();
        snapshot_.error_code = std::move(error_code);
        if (changed) {
            ++snapshot_.generation;
        }
    }

    std::atomic_bool stop_requested_{false};
    std::thread worker_{};
    std::mutex retry_mutex_{};
    std::condition_variable retry_wakeup_{};
#endif
    mutable std::mutex mutex_{};
    AudioDeviceSnapshot snapshot_{};
};

MediaRuntime::MediaRuntime()
    : device_monitor_(std::make_shared<DeviceMonitor>()),
      audio_device_monitor_(std::make_shared<AudioDeviceMonitor>()) {}

MediaRuntime::~MediaRuntime() {
    device_monitor_->stop();
    audio_device_monitor_->stop();
}

MediaRuntimeProbe MediaRuntime::initialize() {
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
    if (initialized_) {
        probe_.d3d11_compositor =
            has_element_factory("d3d11compositor") &&
            static_cast<bool>(gstreamer_d3d11_device(source_runtime_factory_));
        probe_.virtual_camera = probe_platform_virtual_camera();
        return probe_;
    }
    // GStreamer cannot be safely deinitialized and initialized again in one process.
    // The media sidecar therefore owns one process-lifetime session, while shared
    // session tokens keep that ownership explicit across factories, runtimes and frames.
    static std::mutex process_session_mutex;
    static std::shared_ptr<RuntimeSession> process_session;
    {
        std::scoped_lock lock{process_session_mutex};
        if (process_session == nullptr) {
            auto candidate = std::make_shared<RuntimeSession>();
            GError* error = nullptr;
            if (gst_init_check(nullptr, nullptr, &error) == FALSE) {
                if (error != nullptr) {
                    g_error_free(error);
                }
                return {};
            }
            process_session = std::move(candidate);
        }
        runtime_session_ = process_session;
    }
    try {
        source_runtime_factory_ = make_gstreamer_source_runtime_factory(
            runtime_session_, true,
            [monitor = device_monitor_](const std::string_view device_id)
                -> std::optional<LocalCameraSourceConfiguration> {
                if (device_id.empty()) {
                    return std::nullopt;
                }
                const auto snapshot = monitor->snapshot();
                const auto device = std::ranges::find_if(
                    snapshot.devices,
                    [device_id](const LocalCameraDevice& candidate) {
                        return candidate.device_id == device_id;
                    });
                if (device == snapshot.devices.end()) {
                    return std::nullopt;
                }
                const auto format = preferred_automatic_camera_format(*device);
                if (!format.has_value()) {
                    return std::nullopt;
                }
                return LocalCameraSourceConfiguration{
                    .device_id = device->device_id,
                    .width = format->width,
                    .height = format->height,
                    .fps_numerator = format->fps_numerator,
                    .fps_denominator = format->fps_denominator,
                    .media_type = format->media_type,
                    .pixel_format = format->pixel_format,
                };
            });
    } catch (const std::exception&) {
        runtime_session_.reset();
        return {};
    }
    initialized_ = true;
    const bool common_source_path =
        has_element_factory("appsink") && has_element_factory("capsfilter") &&
        has_element_factory("videoconvert");
    probe_ = {
        .initialized = true,
        .local_camera_source = common_source_path &&
                               has_element_factory("mfvideosrc") &&
                               has_element_factory("decodebin3"),
        .rtsp_source = common_source_path && has_element_factory("rtspsrc") &&
                       has_element_factory("uridecodebin3"),
        .d3d11_compositor =
            has_element_factory("d3d11compositor") &&
            static_cast<bool>(gstreamer_d3d11_device(source_runtime_factory_)),
        .audio_capture = has_element_factory("wasapi2src"),
        .program_recording = program_recording_runtime_supported(),
        .virtual_camera = probe_platform_virtual_camera(),
        .version = runtime_version(),
    };
    if (probe_.local_camera_source) {
        device_monitor_->start();
    } else {
        device_monitor_->mark_unavailable("local_camera_source_unavailable");
    }
    if (probe_.audio_capture) {
        audio_device_monitor_->start();
    } else {
        audio_device_monitor_->mark_unavailable("audio_capture_unavailable");
    }
    return probe_;
#else
    return {};
#endif
}

LocalCameraSnapshot MediaRuntime::local_cameras() const { return device_monitor_->snapshot(); }

AudioDeviceSnapshot MediaRuntime::audio_devices() const {
    return audio_device_monitor_->snapshot();
}

std::shared_ptr<SourceRuntimeFactory> MediaRuntime::source_runtime_factory() const {
    if (!initialized_ || source_runtime_factory_ == nullptr) {
        throw std::logic_error("media runtime is not initialized");
    }
    return source_runtime_factory_;
}

std::shared_ptr<SceneRenderer> MediaRuntime::scene_renderer() const {
    if (!initialized_) {
        throw std::logic_error("media runtime is not initialized");
    }
    try {
        return make_gstreamer_scene_renderer(source_runtime_factory_);
    } catch (const SceneRendererError&) {
        return {};
    }
}

bool MediaRuntime::is_compiled() noexcept {
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
    return true;
#else
    return false;
#endif
}

} // namespace solin::media_engine
