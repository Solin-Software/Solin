#pragma once

#include "solin/media_engine/scene_renderer.hpp"
#include "solin/media_engine/scene_snapshot.hpp"

#include <memory>
#include <optional>

namespace solin::media_engine {

class FrameChannelOutputController final {
  public:
    FrameChannelOutputController(std::shared_ptr<SceneRenderer> renderer,
                                 OutputBus bus);
    ~FrameChannelOutputController();

    FrameChannelOutputController(const FrameChannelOutputController&) = delete;
    FrameChannelOutputController& operator=(const FrameChannelOutputController&) =
        delete;
    FrameChannelOutputController(FrameChannelOutputController&&) = delete;
    FrameChannelOutputController& operator=(FrameChannelOutputController&&) =
        delete;

    [[nodiscard]] bool configure(
        const std::optional<FrameChannelConfiguration>& channel,
        const SceneOutputDefinition& output) noexcept;
    [[nodiscard]] bool set_enabled(bool enabled) noexcept;
    void shutdown() noexcept;

  private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace solin::media_engine
