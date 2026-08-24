#pragma once

#include "solin/media_engine/source_registry.hpp"
#include "solin/media_engine/scene_renderer.hpp"
#include "solin/media_engine/virtual_camera.hpp"

#include <cstdint>
#include <memory>
#include <optional>
#include <string>
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

struct LocalCameraDevice {
    std::string device_id{};
    std::string display_name{};
    bool software_device{false};
    std::vector<LocalVideoFormat> formats{};

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

struct MediaRuntimeProbe {
    bool initialized{false};
    bool local_camera_source{false};
    bool rtsp_source{false};
    bool d3d11_compositor{false};
    VirtualCameraBackendProbe virtual_camera{};
    std::string version{};
};

class MediaRuntime final {
  public:
    MediaRuntime();
    ~MediaRuntime();

    MediaRuntime(const MediaRuntime&) = delete;
    MediaRuntime& operator=(const MediaRuntime&) = delete;
    MediaRuntime(MediaRuntime&&) = delete;
    MediaRuntime& operator=(MediaRuntime&&) = delete;

    [[nodiscard]] MediaRuntimeProbe initialize();
    [[nodiscard]] LocalCameraSnapshot local_cameras() const;
    [[nodiscard]] std::shared_ptr<SourceRuntimeFactory> source_runtime_factory() const;
    [[nodiscard]] std::shared_ptr<SceneRenderer> scene_renderer() const;
    [[nodiscard]] static bool is_compiled() noexcept;

  private:
    class DeviceMonitor;
    class RuntimeSession;

    bool initialized_{false};
    MediaRuntimeProbe probe_{};
    std::shared_ptr<RuntimeSession> runtime_session_{};
    std::shared_ptr<SourceRuntimeFactory> source_runtime_factory_{};
    std::shared_ptr<DeviceMonitor> device_monitor_;
};

} // namespace solin::media_engine
