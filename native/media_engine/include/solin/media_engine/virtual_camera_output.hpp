#pragma once

#include "solin/media_engine/scene_renderer.hpp"
#include "solin/media_engine/scene_snapshot.hpp"
#include "solin/media_engine/virtual_camera.hpp"

#include <memory>

namespace solin::media_engine {

class VirtualCameraOutputController final {
  public:
    VirtualCameraOutputController(
        std::shared_ptr<SceneRenderer> renderer,
        std::unique_ptr<VirtualCameraBackend> backend);
    ~VirtualCameraOutputController();

    VirtualCameraOutputController(const VirtualCameraOutputController&) = delete;
    VirtualCameraOutputController& operator=(
        const VirtualCameraOutputController&) = delete;
    VirtualCameraOutputController(VirtualCameraOutputController&&) = delete;
    VirtualCameraOutputController& operator=(VirtualCameraOutputController&&) =
        delete;

    [[nodiscard]] bool configure(
        const SceneOutputDefinition& output) noexcept;
    [[nodiscard]] bool set_enabled(bool enabled) noexcept;
    [[nodiscard]] VirtualCameraBackendProbe probe() const noexcept;
    [[nodiscard]] VirtualCameraSinkHealth health() const noexcept;
    void shutdown() noexcept;

  private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace solin::media_engine
