#pragma once

#include "solin/media_engine/scene_renderer.hpp"
#include "solin/media_engine/source_registry.hpp"

#include <memory>

namespace solin::media_engine {

[[nodiscard]] std::shared_ptr<SceneRenderer> make_gstreamer_scene_renderer(
    std::shared_ptr<SourceRuntimeFactory> source_factory);

} // namespace solin::media_engine
