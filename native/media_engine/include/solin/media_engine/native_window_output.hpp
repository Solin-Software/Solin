#pragma once

#include "solin/media_engine/scene_renderer.hpp"
#include "solin/media_engine/scene_snapshot.hpp"

#include <memory>
#include <vector>

namespace solin::media_engine {

class NativeWindowOutputController final {
  public:
    explicit NativeWindowOutputController(std::shared_ptr<SceneRenderer> renderer);
    ~NativeWindowOutputController();

    NativeWindowOutputController(const NativeWindowOutputController&) = delete;
    NativeWindowOutputController& operator=(const NativeWindowOutputController&) = delete;

    [[nodiscard]] bool configure(const std::vector<OutputWindowConfiguration>& targets) noexcept;
    [[nodiscard]] bool set_enabled(bool enabled) noexcept;
    void shutdown() noexcept;

  private:
    class Impl;
    std::unique_ptr<Impl> impl_{};
};

} // namespace solin::media_engine
