#pragma once

#include "solin/media_engine/local_camera_discovery.hpp"
#include "solin/media_engine/source_registry.hpp"
#include "solin/media_engine/scene_renderer.hpp"
#include "solin/media_engine/program_recording.hpp"
#include "solin/media_engine/virtual_camera.hpp"

#include <memory>
#include <string>

namespace solin::media_engine {

struct MediaRuntimeProbe {
    bool initialized{false};
    bool local_camera_source{false};
    bool rtsp_source{false};
    bool d3d11_compositor{false};
    bool audio_capture{false};
    bool program_recording{false};
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
    [[nodiscard]] AudioDeviceSnapshot audio_devices() const;
    [[nodiscard]] std::shared_ptr<SourceRuntimeFactory> source_runtime_factory() const;
    [[nodiscard]] std::shared_ptr<SceneRenderer> scene_renderer() const;
    [[nodiscard]] static bool is_compiled() noexcept;

  private:
    class DeviceMonitor;
    class AudioDeviceMonitor;
    class RuntimeSession;

    bool initialized_{false};
    MediaRuntimeProbe probe_{};
    std::shared_ptr<RuntimeSession> runtime_session_{};
    std::shared_ptr<SourceRuntimeFactory> source_runtime_factory_{};
    std::shared_ptr<DeviceMonitor> device_monitor_;
    std::shared_ptr<AudioDeviceMonitor> audio_device_monitor_;
};

} // namespace solin::media_engine
